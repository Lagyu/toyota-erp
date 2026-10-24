# Backup, restore and restore drills

Nightly encrypted off-host backups, the weekly restore drill and its evidence, real restores with `scripts/deploy/restore.sh`, disaster recovery onto a new host, and the recovery objectives.

Sources:

- `ops/backup/backup.sh`, `ops/backup/restore-drill.sh`, `ops/backup/adopt_encryption_key.py`
- `scripts/deploy/restore.sh`
- `apps/automotive_core/automotive_core/ops/backup_checks.py`
- the `scheduler` and `cron` services in `infra/deploy/compose.hinode.yaml`

```bash
ENV=/etc/hinode/production.env
SITE=erp.hinode.example
```

## Recovery objectives and evidence

The brief (§7) asks for RPO ≤ 15 minutes and RTO ≤ 2 hours where the architecture permits. They may be claimed only after measured drills.

| Objective | What the delivered tooling gives | Status |
|---|---|---|
| RPO ≤ 15 min | One off-host snapshot per night, so losing the host loses up to ~24 h of data. MariaDB binary logs (ROW, 14 days) are kept **on the same host** in the `db-data` volume. They are not shipped off-host, and no point-in-time-recovery tooling exists ([database.md](database.md#binary-logs-and-point-in-time-recovery)). | **Not met** |
| RTO ≤ 2 h | Restore time is measured by every drill: `restore_seconds` in the evidence, and the metric `erp_restore_drill_duration_seconds`. | **Not validated on real data** |

**Measured so far:**

- The CI smoke test of 2026-10-24 (`scripts/ci/smoke.sh`, evidence `.local/smoke/20261024T022605Z/`) ran one backup into a local restic repository and a restore drill. The **drill passed in 44 s on the empty smoke site** (snapshot `6fb1b416`).
- **Nothing has been measured on a standard-size dataset yet.** Before claiming an RTO, run drills on a staging stack loaded with production-size data and record the times.

**History note.** Before the fix of 2026-10-24, restores and drills looked only for `.tar` file archives. `bench backup --compress` writes `.tgz`, so attachments were silently not restored, and drills did not check them. The 44 s drill above ran before this fix, and before the *attachments restored* and *encrypted fields decrypt* checks existed.

## What a backup contains and where it goes

**Schedule.** The `cron` service runs the jobs as user `frappe` with the scheduler's environment and secrets (`ops/backup/cron.py` on the application image; it needs no Docker socket):

| Job | Schedule variable | Default (UTC, seconds first) | Command |
|---|---|---|---|
| `backup` | `BACKUP_SCHEDULE` | `0 30 17 * * *`: 02:30 JST daily | `bash /opt/hinode/ops/backup/backup.sh` |
| `restore-drill` | `RESTORE_DRILL_SCHEDULE` | `0 30 19 * * 6`: Sunday 04:30 JST | `bash /opt/hinode/ops/backup/restore-drill.sh` |

`/opt/hinode/ops/backup` is a read-only bind mount of the repository's `ops/backup/`. `scripts/deploy/upgrade.sh` also runs `backup.sh` before every upgrade.

**`backup.sh` steps**, in `/home/frappe/frappe-bench`, stopping at the first failure:

1. `bench --site <site> backup --with-files --compress` writes a set in `sites/<site>/private/backups/`:
   - `<YYYYMMDD_HHMMSS>-<site_with_underscores>-database.sql.gz`
   - `…-files.tgz` (public files)
   - `…-private-files.tgz`
   - `…-site_config_backup.json`

   The site config backup includes the site's `encryption_key` and database credentials. Only `public/files` and `private/files` are archived. Other directories under the site (integration file drop, `private/ops`) are **not** backed up.
2. `restic init` if the repository does not exist yet (log: `initialising repository`).
3. `restic backup --host <site> --tag site:<site> --tag ${BACKUP_TAG:-nightly}` of the set. restic encrypts client-side (AES-256) with the password in `RESTIC_PASSWORD_FILE`.
4. `restic forget --host <site> --tag site:<site> --group-by host,tags --prune` with:
   - `--keep-daily ${RESTIC_KEEP_DAILY:-14}`
   - `--keep-weekly ${RESTIC_KEEP_WEEKLY:-8}`
   - `--keep-monthly ${RESTIC_KEEP_MONTHLY:-12}`

   Grouping by tags gives snapshots with another `BACKUP_TAG`, such as `pre-upgrade`, their own retention.
5. `restic check --read-data-subset=5%`: reads back 5% of the repository data.
6. Writes the success marker `sites/<site>/private/ops/backup-last-success.txt` (UTC time). The metrics endpoint exposes it as `erp_backup_last_success_timestamp_seconds`.
7. Deletes local set files older than 3 days. `bench backup` itself also prunes `private/backups` by age: `keep_backups_for_hours` is 72, set by `new-site.sh`.
8. Logs `done: snapshot <short id> <time>`.

**Off-host repository.**

- `RESTIC_REPOSITORY` is an S3-compatible bucket at another site. The production example uses `s3:https://backup.example.net/hinode-erp-production`.
- Credentials: `RESTIC_AWS_ACCESS_KEY_ID` / `RESTIC_AWS_SECRET_ACCESS_KEY`, passed to `scheduler` and `cron` as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.
- The reference example keeps its repository in the sites volume (`/home/frappe/frappe-bench/sites/.restic-reference`). That is **not off-host** and is acceptable only for rebuildable reference data; `preflight.sh` refuses it for production.
- **Protecting the copies.** `scheduler` and `cron` hold credentials that can also delete snapshots: the backup runs `forget --prune`. Protecting the off-host copies against a compromised host needs storage-side features such as bucket versioning or object lock. These are **not configured by this repository**: set them up on the storage side.
- **Escrow** the restic password off the host. Without it no snapshot can be decrypted.

## Check backup health

```bash
# schedules at start, job runs, their output and exit status
./scripts/deploy/compose.sh "$ENV" logs --tail 100 cron
# time of the last successful backup
./scripts/deploy/compose.sh "$ENV" exec -T scheduler cat "sites/$SITE/private/ops/backup-last-success.txt"
# snapshots in the off-host repository
./scripts/deploy/compose.sh "$ENV" exec -T scheduler restic snapshots --host "$SITE"
# newest drill evidence (SITE_NAME is set inside the scheduler container)
./scripts/deploy/compose.sh "$ENV" exec -T scheduler bash -c \
  'cat "$(ls -t sites/$SITE_NAME/private/ops/drills/*.json | head -1)"'
```

Alerts ([monitoring.md](monitoring.md)):

- **Backups:** `ErpBackupStale` (no successful backup for 26 h) and `ErpBackupNeverRan` (no marker for 30 h).
- **Drills:** `ErpRestoreDrillFailed` (the last drill failed) and `ErpRestoreDrillStale` (no drill for 8 days).

The drill alerts read `sites/<site>/private/ops/restore-drill-last.json`. Every drill writes it on exit, pass or fail, with `finished_at`, `ok`, `seconds` and `snapshot`.

**Run a backup now:**

```bash
./scripts/deploy/compose.sh "$ENV" exec -T scheduler bash /opt/hinode/ops/backup/backup.sh
```

Expected output:

```text
... [backup] bench backup <site>
... [backup] backup set: <ts>-<slug>-database.sql.gz <ts>-<slug>-files.tgz <ts>-<slug>-private-files.tgz <ts>-<slug>-site_config_backup.json
... [backup] done: snapshot 1a2b3c4d 2026-10-24T17:30:41
```

## Weekly restore drill

`ops/backup/restore-drill.sh` proves that the newest snapshot can actually be restored. It works on the same stack, using a scratch site `restore-drill.invalid` on the same MariaDB, so run it off-peak (the default schedule is Sunday 04:30 JST).

1. Restores the newest snapshot of the site (`restic snapshots --host <site> --latest 1`) into a temporary directory.
2. Finds the dump and the public and private file archives (`.tar` or `.tgz`). It **stops with an error if the snapshot has no file archives**.
3. Strips trigger `DEFINER` clauses from the dump, so the scratch site's own database user can load it.
4. Creates the scratch site with a random Administrator password.
5. Adopts the source site's encryption key from the snapshot's `*-site_config_backup.json` (`ops/backup/adopt_encryption_key.py`, file to file).
6. Runs `bench restore` with both archives, then `bench migrate`.
7. Runs `automotive_core.ops.backup_checks.verify_restored_site` on the scratch site:

   | Check | Passes when |
   |---|---|
   | `trial balance <company>` | GL debits = credits per company (0.01) |
   | `audit hash chain` | the restored audit chain verifies end to end |
   | `record counts` | at least one Company. Also reports counts of User, Item, BOM, Work Order, Purchase Order, Sales Invoice, GL Entry, Stock Ledger Entry, Audit Event, Vehicle Unit. |
   | `attachments restored` | every `/files/` and `/private/files/` File record exists on disk (count, up to 5 missing examples) |
   | `encrypted fields decrypt` | a sample of up to 50 encrypted fields decrypts with the adopted key |
   | `latest posting` | always ok; reports the newest GL posting date |

8. Writes the evidence `sites/<site>/private/ops/drills/<YYYYMMDDTHHMMSSZ>.json`:

   ```json
   {"snapshot": "…", "source_site": "…", "restore_seconds": 44, "checks": {"ok": true, "checks": [ … ]}}
   ```

9. Prints `ok` or `FAIL` per check and `... [drill] restore drill passed in <N>s (evidence …)`. The exit status is 1 if any check fails.
10. On every exit, pass or fail:
    - writes the outcome marker `sites/<site>/private/ops/restore-drill-last.json`, read by the metrics endpoint (`erp_restore_drill_*`);
    - drops the scratch site. The archived copy that `drop-site` keeps goes into the temporary directory, which is removed.

Run a drill by hand:

```bash
./scripts/deploy/compose.sh "$ENV" exec -T scheduler bash /opt/hinode/ops/backup/restore-drill.sh
```

**Evidence retention.**

- Drill evidence stays in the sites volume and is not pruned.
- It is **not** part of the backups. Copy the JSON files to your evidence store if you need them beyond the life of the host.

## Restore a site (restore.sh)

`./scripts/deploy/restore.sh` replaces **all** data of the environment's site with a snapshot. Use it for:

- recovery from data corruption or operator error;
- an upgrade rollback ([upgrade.md](upgrade.md#rollback));
- disaster recovery (below).

```bash
./scripts/deploy/restore.sh "$ENV"                 # newest snapshot of this site
./scripts/deploy/restore.sh "$ENV" <snapshot id>   # a specific snapshot (restic snapshots ... shows the ids)
```

1. Asks you to **type the site name** to confirm. Anything else aborts.
2. `bench --site <site> set-maintenance-mode on`.
3. In `scheduler`:
   - `restic restore` of the snapshot;
   - strip DEFINERs;
   - find the archives (`.tar`/`.tgz`; **fails if none**);
   - `bench --site <site> restore <dump> --with-public-files … --with-private-files … --force`;
   - adopt the backup's encryption key into the site config.
4. `bench --site <site> migrate`.
5. `bench --site <site> set-maintenance-mode off`.
6. `scripts/deploy/smoke.sh`, then `... [restore] restored <site> from <snapshot>`.

**Verify:**

- The smoke output shows all checks `ok`.
- Users can log in. 2FA users keep their OTP enrolment, thanks to the adopted encryption key.
- Spot-check recent documents and attachments.

**Failure handling.**

- The script stops at the first error and **leaves maintenance mode on**. Read the output; restic and bench errors are printed.
- Fix the cause, for example repository access, a wrong snapshot id or disk space, and run it again.
- Turn maintenance off only when the site is consistent:

  ```bash
  ./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" set-maintenance-mode off
  ```

**Data written after the snapshot is lost.** Only whole-site restores exist: restoring single documents or files is **not implemented**.

## Disaster recovery onto a new host

When the host is lost:

1. Install a new host as in [install-fresh-host.md](install-fresh-host.md), steps 1–7. Use the **same** values for:
   - `SITE_NAME`;
   - `RESTIC_REPOSITORY` and the S3 credentials;
   - the **restic password** (from escrow).

   The other secrets may be new.
2. Create the empty site: `./scripts/deploy/new-site.sh "$ENV"`.
3. Restore the newest snapshot: `./scripts/deploy/restore.sh "$ENV"`. The backup's `encryption_key` is adopted, so passwords, API secrets, partner signing secrets and 2FA secrets still decrypt.
4. Restart the application services so every process reads the restored configuration:

   ```bash
   ./scripts/deploy/compose.sh "$ENV" restart backend frontend websocket queue-short queue-long scheduler
   ```

5. Move DNS to the new host, and install or obtain the TLS certificate.
6. Verify:
   - `./scripts/deploy/smoke.sh "$ENV"`;
   - a manual drill;
   - the audit chain ([monitoring.md](monitoring.md#audit-chain-verification));
   - the integration reconciliation ([integration-operations.md](integration-operations.md#reconciliation));
   - `bench --site "$SITE" scheduler status`.
7. Record the time from step 1 to step 6 as your measured RTO.

Lost: everything after the last nightly snapshot. Also lost: unprocessed files in the integration file-drop directories, which are not in backups.

## Related

- [upgrade.md](upgrade.md): pre-upgrade backup and rollback
- [database.md](database.md)
- [monitoring.md](monitoring.md)
- [troubleshooting.md](troubleshooting.md#backup-failure-alerts)
