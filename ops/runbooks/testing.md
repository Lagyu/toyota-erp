# Testing

How to run each test suite, where reports go, and how the suites map to the CI workflow `.github/workflows/ci.yml`. Every CI job calls the same script a developer or operator runs locally.

| Suite | Command | Needs | Output / reports |
|---|---|---|---|
| Lint | `./scripts/ci/lint.sh` | Python 3 | console |
| Unit | `./scripts/ci/unit.sh` | Python 3 | console |
| Integration | `./scripts/dev/up.sh`, then `./scripts/dev/test-all.sh [app ...]` (CI: `./scripts/ci/integration.sh`) | Docker; development stack | `.local/dev-state/logs/test-<app>.log` |
| End to end | `./scripts/ci/e2e.sh IMAGE:TAG` or `--url URL --site SITE` | Docker; reference image or a seeded dev site | `.local/e2e/` |
| Production-stack smoke | `./scripts/ci/smoke.sh IMAGE:TAG` | Docker; production image | `.local/smoke/<timestamp>/` |
| Deployed-environment smoke | `./scripts/deploy/smoke.sh ENV_FILE` | a running stack | console ([install-fresh-host.md](install-fresh-host.md#9-smoke-test)) |
| Dependency audit | `./scripts/ci/audit.sh` | Python 3 | console, `.local/constraints-audit.txt` |
| SBOM and image scan | `./scripts/ci/sbom.sh IMAGE` | Docker | `.local/sbom/` |

**CI tools.** `scripts/ci/tools.sh` installs pinned tools into `.local/tools/venv` (override with `CI_VENV`):

- ruff 0.14.1
- shellcheck-py 0.10.0.1
- pip-audit 2.9.0
- PyYAML 6.0.3

The tool images (Syft, Trivy, Playwright) are pinned in `infra/ci-tools.env`.

## Lint

`./scripts/ci/lint.sh` runs every step and fails if any step failed:

1. `ruff check apps scripts`.
2. `shellcheck -x -S warning` on every `*.sh` under `scripts/` and `ops/`.
3. Schema and desk JSON are generated from the specs: `python3 scripts/dev/schema/generate.py --check`.
4. The OpenAPI contract matches the whitelisted methods: `python3 scripts/dev/openapi.py --check`. The output is `apps/automotive_core/automotive_core/integration_hub/openapi.json`.
5. YAML and JSON syntax of every file, except `.git`, `.local`, `node_modules` and `dist`.
6. Identity boundary: `python3 scripts/ci/identity_check.py`, so no protected third-party names appear in customer-facing assets.

The translation-catalogue checker `scripts/dev/check_po.py` (`[--require-complete] apps/*/*/locale/ja.po`, needs Babel) is **not part of lint or CI yet**.

## Unit

```bash
./scripts/ci/unit.sh
```

This runs `python3 -s -m unittest automotive_manufacturing.tests.test_engine -v` in `apps/automotive_manufacturing`. It covers the pure engines (MRP planning, line sequencing, option rules, VIN) without Frappe or a database.

## Integration (real ERPNext, MariaDB, Redis)

The development stack is `infra/dev/compose.yaml`: MariaDB, Redis cache and queue, a `bench` container with the repository mounted at `/workspace`, and a `worker`.

```bash
./scripts/dev/init-env.sh        # once: .local/dev.env with random DB/admin passwords and DEV_* settings
./scripts/dev/up.sh              # start the stack; the bench bootstrap runs detached
tail -f .local/dev-state/logs/bootstrap.log      # wait for "BOOTSTRAP OK (apps: ...)"
./scripts/dev/test-all.sh                         # all apps, or name some: ./scripts/dev/test-all.sh automotive_core
```

**The bootstrap** (`scripts/dev/bootstrap-bench.sh`, inside the bench container) is idempotent and resumable: each finished step leaves a marker in `.local/dev-state/markers/`. It:

1. initialises the bench with Frappe at the pinned commit, and adds ERPNext at its pinned commit;
2. links and installs the custom apps;
3. creates the development site `erp.localhost` and the test site `test.localhost` (with `allow_tests` 1);
4. migrates both sites and builds the assets.

**`test-all.sh`** runs `bench --site test.localhost run-tests --app <app>` for each of `automotive_core`, `automotive_manufacturing`, `automotive_supply_chain`, `automotive_portals` and `automotive_seed`. Each app has a 3000 s timeout. It prints one line per app: `<app> rc=<code> Ran N tests ... OK|FAILED`. Full logs are in `.local/dev-state/logs/test-<app>.log`. The exit status is non-zero if any app failed.

**`scripts/ci/integration.sh`** chains three steps:

1. `up.sh`, which moves the previous `bootstrap.log` to `bootstrap.log.1` and starts the bootstrap detached;
2. `scripts/lib/wait-for-log.sh .local/dev-state/logs/bootstrap.log 'BOOTSTRAP OK' '\[bootstrap\] ERROR' 3600`. A failing bootstrap step writes the `ERROR` line through an ERR trap;
3. `test-all.sh`.

Run it as a one-command equivalent of the steps above.

**Development notes:**

- The dev site is served on `127.0.0.1:18000` once you run `bench --site <site> serve --port 8000` in the bench container (see README).
- The dev worker processes all queues. The scheduler stays off in development; run `bench --site <site> trigger-scheduler-event <event>` to exercise scheduled jobs.
- Seeding a dev site: [seed-reference-data.md](seed-reference-data.md#development-bench-scriptsseed).

## End to end (Playwright)

The browser suite is in `e2e/`. Specs in `e2e/tests/`:

- the ten journeys, `j01-…` to `j10-…`;
- `roles`;
- `portal-supplier` and `portal-dealer`;
- `shop-floor-andon`.

**Playwright configuration** (`e2e/playwright.config.ts`):

- one worker, not parallel; the journeys share one dataset and clock;
- 180 s per test; 1 retry under CI;
- projects `desktop` (Desktop Chrome) and `tablet` (iPad Pro 11 landscape, shop-floor and portal specs);
- trace, screenshot and video kept on failure.

Pass Playwright arguments after `--`, for example `-- tests/j02-kanban-supply.spec.ts` or `-- --project desktop`.

### Mode 1: against a running site (development bench)

```bash
./scripts/ci/e2e.sh --url http://small.localhost:18000 --site small.localhost [-- playwright args]
```

The site must hold the small dataset and be served. Persona passwords and journey fixtures are prepared through the dev bench container, using `automotive_seed.e2e.prepare_users` and `prepare_journeys`.

### Mode 2: a reference stack from an image (what CI runs)

```bash
./scripts/ci/e2e.sh toyota-erp/erp:<version>-<sha>-reference [--keep] [-- playwright args]
```

1. **Stack.** Starts project `hinode-e2e` with `ENVIRONMENT=reference`, `TLS_MODE=none` on `127.0.0.1:18091`, ClamAV and monitoring off, and a local restic repository. Random secrets go to `.local/e2e-stack/<timestamp>/secrets/`.
2. **Site.** Runs `new-site.sh`.
3. **Dataset.** Loads the **small dataset**:
   - It restores `.local/reference/small-<key>/` if present. The key is a hash of the generator inputs: `generator/`, `profiles/`, `reference_data/`, `runner.py`. The restore runs `bench migrate` (forward migration of a dataset from an earlier build).
   - Otherwise it generates the dataset (`automotive_seed.runner.run`, profile `small`) and caches the dump, the `.tgz` file archives and the encryption key.
4. **Scheduler.** Disables the scheduler, so background jobs cannot race the journeys, and restarts backend and frontend.
5. **Suite.** Prepares personas and fixtures, then runs Playwright in the pinned image.
6. **Teardown.** Saves `compose.log` and removes the stack with its volumes. With `--keep`, the stack and its secrets stay up for debugging; the command to reach it is printed.

**Reports** (both modes) in `.local/e2e/`:

- `report/`: Playwright HTML report;
- `junit.xml`;
- `credentials.json` and `fixtures.json`, the one-run persona passwords and fixtures (mode 0600).

## Production-stack smoke

```bash
./scripts/ci/smoke.sh toyota-erp/erp:<version>-<sha>
```

It rehearses a production-shaped install on the current host:

1. Starts project `hinode-smoke`:
   - `ENVIRONMENT=staging`;
   - `TLS_MODE=none` on `127.0.0.1:18090`;
   - ClamAV and monitoring off, small resource limits;
   - a local restic repository in the sites volume.
2. Runs `new-site.sh`, then restarts backend and frontend.
3. Runs `scripts/deploy/smoke.sh`.
4. Runs one real `backup.sh` and a `restore-drill.sh`.
5. Tears everything down.

Evidence goes to `.local/smoke/<timestamp>/`:

- `checks.log`: smoke lines, backup and drill tail;
- `restore-drill.json`;
- `compose.log`;
- `smoke.env.redacted`: the env file without lines mentioning passwords, tokens or secrets.

The secrets are deleted. The run of 2026-10-24 is the recovery evidence cited in [backup-restore.md](backup-restore.md#recovery-objectives-and-evidence).

## Dependency audit, SBOM

See [security-operations.md](security-operations.md#dependency-audit-and-the-allowlist) and [security-operations.md](security-operations.md#sbom-and-image-scan).

## CI workflow mapping

`.github/workflows/ci.yml` runs on pushes to `main`, on `v*` tags and on pull requests. Concurrent runs per ref are cancelled, and token permissions are `contents: read`.

| Job | Needs | Runs | Artifacts |
|---|---|---|---|
| `lint` | | `./scripts/ci/lint.sh` | |
| `unit` | | `./scripts/ci/unit.sh` | |
| `audit` | | `./scripts/ci/audit.sh` | |
| `integration` (90 min) | lint, unit | `./scripts/ci/integration.sh` | `integration-logs`: `.local/dev-state/logs/`, 14 days |
| `image` (150 min, `packages: write`) | integration, audit | see below | `image-evidence`: `.local/sbom/`, `.local/e2e/`, `.local/smoke/`, 30 days |

The `image` job:

1. builds the production and reference images with `scripts/image/build.sh` (and `--reference`);
2. runs `scripts/ci/sbom.sh` and `scripts/ci/smoke.sh` on the production image;
3. restores the `.local/reference` cache;
4. runs `scripts/ci/e2e.sh` on the reference image;
5. on `v*` tags, pushes the production image to `ghcr.io/<owner>/hinode-erp`.

## Not covered by automated tests yet

- **Load and performance tests.** The brief's 50/150-user targets are not measured yet; see [monitoring.md](monitoring.md#erpreadlatencyhigh--erpwritelatencyhigh).
- **Upgrade tests from a previous release.** The only automated forward migration is e2e mode 2 restoring a cached dataset; see [upgrade.md](upgrade.md#schema-migrations-and-data-backfills).
- **The monitoring overlay**, and the TLS modes `files` and `letsencrypt`. Smoke and e2e run with `MONITORING=0` and `TLS_MODE=none`.
- **Reboot persistence.** Rehearse it by hand ([install-fresh-host.md](install-fresh-host.md#11-reboot-persistence)).

## Related

- [seed-reference-data.md](seed-reference-data.md)
- [upgrade.md](upgrade.md)
- [troubleshooting.md](troubleshooting.md)
