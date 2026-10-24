# Operations

Runbooks for deploying, administering, diagnosing and recovering Toyota ERP Clone, run for the fictional tenant Hinode Automotive Group. They are written for an operator who did not build the system. Each runbook gives:

- purpose and prerequisites;
- the exact commands, with expected output;
- verification;
- failure handling.

## System overview

The system is ERPNext v16 on Frappe v16, at pinned upstream commits (`infra/versions.env`). Five custom Frappe apps extend it:

- `automotive_core`: governance, access, integration hub, finance controls, operations endpoints;
- `automotive_manufacturing`;
- `automotive_supply_chain`;
- `automotive_portals`: supplier and dealer portals;
- `automotive_seed`: the synthetic data generator, in reference images only.

**Images.** All five apps are built into one immutable application image (`scripts/image/build.sh`).

**Stack.** A host runs it with Docker Compose. `scripts/deploy/compose.sh` assembles the official frappe_docker compose files with the overrides in `infra/deploy/`. Services:

- the backend (gunicorn), nginx frontend, socket.io and two RQ worker pools;
- the scheduler;
- MariaDB 11.8 and Redis (cache, plus a persistent queue);
- Traefik for TLS;
- a `cron` service for scheduled backups and restore drills (no Docker socket);
- optionally ClamAV and a Prometheus/Alertmanager/Grafana overlay.

**Networks.** The database and Redis sit on an internal network without outbound access.

**Backups.** Restic sends encrypted backups to an off-host S3-compatible repository nightly. A weekly drill restores the newest snapshot into a scratch site and verifies it.

**Topology.** The delivered topology is a single host. High availability is not packaged (ARCHITECTURE.md §6).

## Conventions

Run every command from the repository checkout on the host, which must match the deployed release. Two variables are used throughout:

```bash
ENV=/etc/hinode/production.env     # environment file (copy of infra/deploy/env/*.env.example)
SITE=erp.hinode.example            # SITE_NAME in that file
./scripts/deploy/compose.sh "$ENV" ps                                     # docker compose for the environment
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" … # bench commands in the stack
```

Desk pages are at `https://<site>/desk/<doctype-in-kebab-case>`, for example `/desk/integration-inbound-message`.

## Runbooks

| Runbook | Use it for |
|---|---|
| [install-fresh-host.md](runbooks/install-fresh-host.md) | Host preparation (Docker, `erp.slice`), secrets and their ownership, environment file, TLS modes, preflight, first start, `new-site.sh`, smoke test, enabling backups/ClamAV/monitoring, reboot persistence |
| [high-availability.md](runbooks/high-availability.md) | The high-availability option: what fails over and how, what the operator provides, what has not been rehearsed |
| [upgrade.md](runbooks/upgrade.md) | Building the image (`build.sh`, tags, `--reference`, `--push`), updating upstream pins, `upgrade.sh`, schema migrations and backfills, maintenance mode, rollback |
| [backup-restore.md](runbooks/backup-restore.md) | What `backup.sh` does, schedules, off-host repository, the weekly restore drill and its evidence, `restore.sh`, disaster recovery, RPO/RTO status |
| [monitoring.md](runbooks/monitoring.md) | Health and metrics endpoints, Prometheus/Alertmanager/Grafana, **meaning and first response of every alert**, job queues, logs and correlation ids, audit chain verification |
| [database.md](runbooks/database.md) | MariaDB settings, immutability triggers and unique keys, DEFINERs in dumps, binary logs, safe access |
| [security-operations.md](runbooks/security-operations.md) | Secrets and rotation, privileged MFA, dependency audit and allowlist, SBOM, file uploads and ClamAV, outbound allowlist, TLS/headers, least privilege |
| [access-review.md](runbooks/access-review.md) | Personas and role profiles, row-level scope, SoD rules and review report, provisioning and deprovisioning, plant and partner access, delegation, periodic review, account recovery |
| [master-data.md](runbooks/master-data.md) | Duplicate rules for items, suppliers and customers, lifecycle and history, bulk import with Data Import, exports |
| [period-close.md](runbooks/period-close.md) | Period Close Checklist (checks, waivers, approval, closing and re-opening an Accounting Period), group consolidation, FX revaluation |
| [integration-operations.md](runbooks/integration-operations.md) | Partners, HMAC signing, inbound endpoint and statuses, retries, Replay / Retry Delivery, reconciliation, file drop, Hinode EDI |
| [seed-reference-data.md](runbooks/seed-reference-data.md) | Synthetic data on reference and staging sites only: profiles, running, resuming, validating, e2e fixtures |
| [testing.md](runbooks/testing.md) | Lint, unit, integration, e2e (both modes), smoke, audit, SBOM; report locations; CI job mapping |
| [troubleshooting.md](runbooks/troubleshooting.md) | Stack will not start, site not resolving, 502/503, migrations, stuck jobs, reposting backlog, failed planning runs, failing inbound messages, disk pressure, backup alerts, login problems |

## Scripts at a glance

| Script | Purpose |
|---|---|
| `scripts/host/install-docker-ubuntu.sh` | Docker Engine, Compose and Buildx from the Ubuntu archive, inside the resource-capped `erp.slice` |
| `scripts/image/build.sh [--reference] [--tag NAME] [--push REGISTRY/REPO]` | Build (and push) the immutable image |
| `scripts/image/freeze-constraints.sh` | Regenerate `infra/image/constraints.txt` from the development bench |
| `scripts/deploy/preflight.sh ENV` | Static checks before deploying: settings, secret ownership, TLS files, restic, image, compose render |
| `scripts/deploy/compose.sh ENV <compose args>` | Docker Compose for an environment |
| `scripts/deploy/new-site.sh ENV` | Create or re-configure the site (idempotent) |
| `scripts/deploy/smoke.sh ENV` | Post-deploy checks |
| `scripts/deploy/upgrade.sh ENV` | Backup, maintenance, new image, migrate, smoke |
| `scripts/deploy/restore.sh ENV [SNAPSHOT]` | Restore a snapshot over the site (destructive, asks for confirmation) |
| `ops/backup/backup.sh`, `ops/backup/restore-drill.sh`, `ops/backup/cron.py` | The jobs (in `scheduler` by hand, in `cron` on their schedules) and their scheduler |
| `scripts/ci/*.sh` | The CI suites ([testing.md](runbooks/testing.md)) |

## Where evidence is written

| Evidence | Location | Written by |
|---|---|---|
| Production-stack smoke runs: check log, restore-drill JSON, compose log, redacted env | `.local/smoke/<timestamp>/` in the checkout | `scripts/ci/smoke.sh` |
| Restore drill results, one JSON per drill (snapshot, source site, restore seconds, every check) | `sites/<site>/private/ops/drills/<timestamp>.json` in the sites volume | `ops/backup/restore-drill.sh` |
| Outcome of the last drill (for the metrics endpoint) | `sites/<site>/private/ops/restore-drill-last.json` | `ops/backup/restore-drill.sh` |
| Time of the last successful backup | `sites/<site>/private/ops/backup-last-success.txt` | `ops/backup/backup.sh` |
| End-to-end reports: HTML report, `junit.xml`, run credentials and fixtures (0600) | `.local/e2e/` | `scripts/ci/e2e.sh` |
| End-to-end stack logs | `.local/e2e-stack/<timestamp>/compose.log` | `scripts/ci/e2e.sh` |
| SBOM (SPDX) and vulnerability scan | `.local/sbom/` | `scripts/ci/sbom.sh` |
| Integration test logs, bench bootstrap log | `.local/dev-state/logs/` | `scripts/dev/test-all.sh`, `scripts/dev/up.sh` |
| CI artifacts | `integration-logs` (14 days), `image-evidence` (`.local/sbom/`, `.local/e2e/`, `.local/smoke/`, 30 days) | `.github/workflows/ci.yml` |

`.local/` is not committed, and drill evidence is not part of the backups. Copy the evidence you need for audits or release records (brief §8.2) to your own evidence store.

## Known limitations (operations)

Each item is detailed in the runbook named.

- **RPO and RTO are measured on test sites only.** Binary logs and new uploads are shipped off the host every 5 minutes and replayed on restore; every drill records its restore time and recovery point. The CI smoke test and the production rehearsal run them on small sites; a production-size measurement is still to be recorded. See [backup-restore.md](runbooks/backup-restore.md).
- **Exercised only by the production rehearsal** (`rehearsal.yml`, not by every CI run): the monitoring overlay and the TLS mode `files`. The mode `letsencrypt` needs a public name and is not exercised. See [monitoring.md](runbooks/monitoring.md) and [install-fresh-host.md](runbooks/install-fresh-host.md).
- **Performance** is measured by `perf.yml` on the standard dataset: see [ops/performance/README.md](performance/README.md) for the recorded results.
- **Not scripted:**
  - rotation of the database root password, the restic password and the Grafana password;
  - creation of the MariaDB monitoring account for the exporter;
  - outgoing email setup.

  See [security-operations.md](runbooks/security-operations.md).
- **Manual steps after install:**
  - switching on virus scanning (*Virus Scan Mode* Required);
  - the Alertmanager receiver.
- **Not implemented yet:**
  - bulk replay or re-scan;
  - inbound handlers for `supplier_invoice` and `shipment_status`;
  - centralized log shipping;
  - network-level egress filtering (findings C-03);
  - restoring a single document or file.
