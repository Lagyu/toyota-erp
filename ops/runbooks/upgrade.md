# Build, deploy, upgrade and roll back

Building the immutable application image, rolling a stack to a new image with `scripts/deploy/upgrade.sh`, schema migrations and data backfills, maintenance mode, and rollback. This page also holds the update procedure for the upstream pins in `infra/versions.env`, which that file, `infra/image/constraints.txt` and `config/security/audit-allowlist.txt` refer to.

Variables used below (see [install-fresh-host.md](install-fresh-host.md#prerequisites)):

```bash
ENV=/etc/hinode/production.env
SITE=erp.hinode.example
```

## Build the image

```bash
./scripts/image/build.sh [--reference] [--tag NAME] [--push REGISTRY/REPO]
```

| Option | Effect |
|---|---|
| (none) | Production flavour: Frappe, ERPNext, `automotive_core`, `automotive_manufacturing`, `automotive_supply_chain`, `automotive_portals` |
| `--reference` | Also includes `automotive_seed`, the synthetic data generator. For reference and staging only, **never production**. Tags get a `-reference` suffix. |
| `--tag NAME` | Image name; default `toyota-erp/erp` |
| `--push REGISTRY/REPO` | Also tags both tags as `REGISTRY/REPO:<tag>` and pushes them |

**What it does:**

1. Loads `infra/versions.env`.
2. Clones and verifies the pinned frappe_docker checkout. It provides the entrypoint and `start.sh`.
3. Builds `infra/image/Containerfile` with `docker buildx` and `--load`, with the repository root as context. `.dockerignore` limits the context to `apps/` and `infra/image/`.

The Containerfile:

- runs `bench init` / `bench get-app` at the pinned refs and **fails unless the checked-out commits equal `FRAPPE_COMMIT` / `ERPNEXT_COMMIT`**;
- installs the apps under the pip constraints of `infra/image/constraints.txt`;
- runs `pip check`, then `bench build --production`;
- removes `.git` directories.

**Tags:**

- `<name>:<version>-<12-char git sha>[-reference]`
- `<name>:<version>[-reference]`

`version` is `__version__` of `apps/automotive_core/automotive_core/__init__.py`, currently `0.1.0`. With uncommitted changes under `apps/` or `infra/image/`, the sha gets a **`-dirty`** suffix. Never deploy a dirty image.

**Labels:**

- `org.opencontainers.image.version`
- `org.opencontainers.image.revision`
- `com.hinode.erp.includes-seed`

**Output:** the last line is the full image reference, for example `toyota-erp/erp:0.1.0-72021b313d05`. Use the `<version>-<sha>` tag as `CUSTOM_TAG` in the environment file.

**CI** (`.github/workflows/ci.yml`, job `image`):

- builds both flavours on every push;
- runs the SBOM/vulnerability scan, the production-stack smoke test and the e2e suite;
- on a `v*` tag, pushes the production flavour with `./scripts/image/build.sh --push "ghcr.io/<owner>/hinode-erp"`.

Before promoting an image, scan it: `./scripts/ci/sbom.sh IMAGE`. See [security-operations.md](security-operations.md#sbom-and-image-scan).

## Updating the upstream pins

Upstream pins live in `infra/versions.env`:

- Frappe, ERPNext and frappe_docker tag + commit;
- image digests for bench, build, base, MariaDB, Redis, Traefik, ClamAV, monitoring and CI tools.

Every clone is verified against the pinned commit (`scripts/lib/verify-checkout.sh`), so a moved tag cannot change a build.

1. Pick the new releases and resolve each tag to its commit, e.g. `git ls-remote https://github.com/frappe/frappe refs/tags/v16.xx.y`.
2. Edit `infra/versions.env`: `*_REF` and `*_COMMIT`, and any image digests you move.
3. Rebuild the development bench at the new pins.
   - The bootstrap keeps markers and refuses a checkout that does not match the pin.
   - Remove the markers in `.local/dev-state/markers/` and run `./scripts/dev/up.sh`. This re-creates the development bench and its sites. Wait for `BOOTSTRAP OK` in `.local/dev-state/logs/bootstrap.log`.
4. Freeze the Python dependencies of the new bench into the image constraints, and review the diff:

   ```bash
   ./scripts/image/freeze-constraints.sh      # "wrote .../infra/image/constraints.txt (N packages)"
   git diff infra/image/constraints.txt
   ```

5. Run `./scripts/ci/audit.sh`. Remove allowlist entries whose advisories are fixed by the new pins, and justify any new ones (see [security-operations.md](security-operations.md#dependency-audit-and-the-allowlist)).
6. Run the full test suite ([testing.md](testing.md)), build both image flavours, and run `scripts/ci/sbom.sh` and `scripts/ci/smoke.sh`.
   - Read the upstream changes to the methods the apps replace (`override_whitelisted_methods` in `automotive_core/hooks.py`: the report download, `frappe.desk.query_report.export_query`). The replacement runs Frappe's own function, so a new parameter keeps working, but a new download path in the desk would not be logged in the Access Log (D-035).
7. Rehearse the upgrade on staging with a recent production backup (below) before production.

## Upgrade procedure

### Before the window

- Read `CHANGELOG.md` and the commits of the release.
- Note new patches in `apps/*/*/patches.txt`, and any tenant changes in `apps/automotive_core/automotive_core/setup/tenants/hinode.yaml`.
- Confirm the last nightly backup and drill are green ([backup-restore.md](backup-restore.md#check-backup-health)).
- Note the **current** `CUSTOM_TAG`: you need it for a rollback.
- Rehearse on staging first. This is the only migration test between releases (see [Schema migrations](#schema-migrations-and-data-backfills)).

### Steps

1. **Update the repository checkout** on the host to the new release tag. Compose overrides, Traefik/monitoring configs and `ops/backup/` come from the checkout. `ops/backup/` is bind-mounted into `scheduler`, so the next backup job already uses the new scripts.

   ```bash
   git fetch --tags && git checkout <new release tag>
   ```

2. **Set `CUSTOM_TAG`** in `$ENV` to the new tag, then run preflight:

   ```bash
   ./scripts/deploy/preflight.sh "$ENV"
   ```

3. **Run the upgrade:**

   ```bash
   ./scripts/deploy/upgrade.sh "$ENV"
   ```

`upgrade.sh` stops at the first failing step (`set -e`):

1. `bench --site <site> set-maintenance-mode on`.
2. **Backup** of the running version: `backup.sh` in the current `scheduler` container, tagged `pre-upgrade`. Taken inside the maintenance window, it holds exactly the state being upgraded. Note the line `... [backup] done: snapshot <id> <time>`: this is your rollback point. `restic forget` groups snapshots by host and tags, so the nightly backups' retention (one snapshot a day) never replaces it with a later backup of the same day; `pre-upgrade` snapshots follow the same retention among themselves.
3. `compose pull --quiet` (failures ignored), then `compose up -d --remove-orphans`, which recreates the containers on the new image.
4. `bench --site <site> migrate`.
5. `bench --site <site> set-maintenance-mode off`.
6. `scripts/deploy/smoke.sh`. All checks `ok`, then `... [upgrade] upgraded <site> to <CUSTOM_TAG>`.

**Downtime** runs from step 1 to step 5, the backup included:

- While `maintenance_mode` is set, Frappe answers **every** request with HTTP 503, including the health endpoints.
- The container health checks turn unhealthy.
- `ErpBackendDown` fires if this lasts more than 5 minutes.

### After the upgrade

- Smoke output is all `ok`. `./scripts/deploy/compose.sh "$ENV" ps` shows the new image on every application service.
- **Tenant definition changes are not applied by `migrate`.** Check for drift, and apply if the release notes ask for it:

  ```bash
  ./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" execute \
    automotive_core.setup.tenant.apply --kwargs "{'tenant': 'hinode', 'dry_run': True}"   # lists what would change
  ./scripts/deploy/new-site.sh "$ENV"                                                      # idempotent: re-applies tenant and site config
  ```

- Watch the Error Log (`/desk/error-log`), the queue metrics and the next nightly backup.

## Schema migrations and data backfills

`bench migrate` (step 4 above) is the only migration mechanism. It runs the patches in each app's `patches.txt`, in this order:

1. `[pre_model_sync]` patches, before the DocType schema sync;
2. the schema sync from the DocType JSON;
3. `[post_model_sync]` patches.

Each patch runs **once per site**; Frappe records it. Frappe then runs the apps' `after_migrate` hooks. For `automotive_core` (`setup/install.py`) these re-apply, idempotently:

- roles and role profiles;
- custom fields;
- least-privilege permissions;
- settings defaults;
- the database guards (unique keys, immutability triggers; [database.md](database.md));
- the KPI catalogue.

| App | Registered patches |
|---|---|
| `automotive_manufacturing` | `automotive_manufacturing.patches.v0_1.backfill_genealogy_links` (post model sync): records genealogy links for submitted Manufacture/Repack stock entries that have none. Idempotent. |
| `automotive_core`, `automotive_supply_chain`, `automotive_portals`, `automotive_seed` | none yet |

- **Data backfills** are written as patches, so they run with the migration and only once.
- **Re-running a backfill:** an idempotent backfill function can also be run by hand, e.g. `bench --site SITE execute automotive_manufacturing.traceability.genealogy.backfill`. It accepts an optional `limit`.
- **Long backfills** extend the maintenance window. Measure them in the staging rehearsal.
- **No down-migrations exist.** Going back means restoring the pre-upgrade snapshot.
- **No dedicated release-to-release migration test.** The only automated forward-migration path is `scripts/ci/e2e.sh` running `bench migrate` on a cached dataset generated by an earlier build.

## Rollback

Use a rollback when the migration failed or the new release misbehaves.

**Fix forward** if the cause is clear and small. A failed migrate leaves **maintenance mode on** with the new image running:

```bash
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" migrate          # after fixing the cause
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" set-maintenance-mode off
./scripts/deploy/smoke.sh "$ENV"
```

**Roll back** to the previous release:

1. Find the pre-upgrade snapshot. Use the id printed by `upgrade.sh`, or list the snapshots:

   ```bash
   ./scripts/deploy/compose.sh "$ENV" exec -T scheduler restic snapshots --host "$SITE"
   ```

2. Check out the previous release tag in the repository checkout, and set `CUSTOM_TAG` in `$ENV` back to the previous value.
3. Recreate the containers on the previous image:

   ```bash
   ./scripts/deploy/compose.sh "$ENV" up -d --remove-orphans
   ```

4. Restore the database and files of the snapshot **as it is**. This is required whenever `migrate` ran even partially, because the schema may already have changed.

   ```bash
   ./scripts/deploy/restore.sh "$ENV" <snapshot id> --no-replay    # asks you to type the site name
   ```

   `--no-replay` matters: by default `restore.sh` brings a backup forward with the shipped binary logs, and those logs hold the upgrade's own migration. The snapshot already holds everything up to the upgrade, because it was taken in the maintenance window. (A previous release older than point-in-time recovery has no such option and does not replay.) It sets maintenance mode on, restores, runs migrate (nothing to do on the old code), sets maintenance mode off and runs the smoke test. See [backup-restore.md](backup-restore.md#restore-a-site-restoresh).
5. Verify the smoke output and the application. Changes made after the snapshot are lost: none before the rollback decision, because maintenance mode was on from before the backup; whatever users entered on the new release in the meantime has to be entered again.

An image-only rollback (steps 2–3 without the restore) is safe only if `migrate` never started.

## Maintenance mode

```bash
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" set-maintenance-mode on    # or off
```

This sets `maintenance_mode` in the site config. Every web request gets HTTP 503 until it is turned off. `upgrade.sh` and `restore.sh` use it. If either stops half-way, maintenance mode **stays on**: turn it off once the site is consistent again.

## Configuration changes without a new image

After editing sizing or other variables in `$ENV`:

```bash
./scripts/deploy/preflight.sh "$ENV"
./scripts/deploy/compose.sh "$ENV" up -d                 # recreates only the services whose configuration changed
./scripts/deploy/compose.sh "$ENV" restart frontend      # if backend or websocket were recreated
./scripts/deploy/smoke.sh "$ENV"
```

## Related

- [install-fresh-host.md](install-fresh-host.md)
- [backup-restore.md](backup-restore.md)
- [testing.md](testing.md)
- [troubleshooting.md](troubleshooting.md#migrations-failing)
