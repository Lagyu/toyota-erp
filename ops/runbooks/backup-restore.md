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
| RPO ≤ 15 min | The nightly backup set records its binary log position; the binary logs are shipped off the host every 5 minutes and replayed onto a restored set ([Point-in-time recovery](#binary-log-shipping-and-point-in-time-recovery)). Losing the host loses the changes since the last shipment: about 5 minutes, plus the copy. Every drill measures it (`rpo_seconds` in the evidence, metric `erp_restore_drill_rpo_seconds`); an alert fires when shipping stops for 20 minutes. | **Designed; measured on test sites only** |
| RTO ≤ 2 h | Restore time is measured by every drill: `restore_seconds` in the evidence, and the metric `erp_restore_drill_duration_seconds`. | **Not validated on real data** |

**Measured so far:**

- Every CI smoke test (`scripts/ci/smoke.sh`, artifact `image-evidence`) runs a backup into a local restic repository, ships the binary logs and runs a drill, and fails unless the drill brought its copy forward to the newest shipped change.
- The production rehearsal of 2026-10-24 (`rehearsal.yml` run 40087480153; restic on S3): backup, one shipment, then a **drill that passed in 40 s with a recovery point of 1.5 s** on the rehearsal's fresh site (all checks ok: trial balances, audit chain, attachments, encrypted fields).
- **Nothing has been measured on a standard-size dataset yet.** Before claiming an RTO or RPO, run drills on a staging stack loaded with production-size data and record the times.

**History note.** Before the fix of 2026-10-24, restores and drills looked only for `.tar` file archives. `bench backup --compress` writes `.tgz`, so attachments were silently not restored, and drills did not check them. Drills recorded before this fix, and before the *attachments restored* and *encrypted fields decrypt* checks existed, prove less than the ones above.

## What a backup contains and where it goes

**Schedule.** The `cron` service runs the jobs as user `frappe` with the scheduler's environment and secrets (`ops/backup/cron.py` on the application image; it needs no Docker socket):

| Job | Schedule variable | Default (UTC, seconds first) | Command |
|---|---|---|---|
| `backup` | `BACKUP_SCHEDULE` | `0 30 17 * * *`: 02:30 JST daily | `bash /opt/hinode/ops/backup/backup.sh` |
| `restore-drill` | `RESTORE_DRILL_SCHEDULE` | `0 30 19 * * 6`: Sunday 04:30 JST | `bash /opt/hinode/ops/backup/restore-drill.sh` |
| `binlog` | `BINLOG_SCHEDULE` | `0 */5 * * * *`: every 5 minutes | `env/bin/python /opt/hinode/ops/backup/binlog.py ship` |

`off` disables a job. The backup and the drill run one at a time; the binary log shipping runs
beside them, so the recovery point keeps moving during a backup. A run missed while the service
was down or busy is not caught up.

`/opt/hinode/ops/backup` is a read-only bind mount of the repository's `ops/backup/`. `scripts/deploy/upgrade.sh` also runs `backup.sh` before every upgrade.

**`backup.sh` steps**, in `/home/frappe/frappe-bench`, stopping at the first failure:

1. `bench --site <site> backup --with-files --compress` writes a set in `sites/<site>/private/backups/`:
   - `<YYYYMMDD_HHMMSS>-<site_with_underscores>-database.sql.gz`
   - `…-files.tgz` (public files)
   - `…-private-files.tgz`
   - `…-site_config_backup.json`

   The site config backup includes the site's `encryption_key` and database credentials. Only `public/files` and `private/files` are archived. Other directories under the site (integration file drop, `private/ops`) are **not** backed up.

   When the database server writes a binary log, the dump runs through `ops/backup/bin/mariadb-dump`, which adds `--master-data=2`: the dump then records the binary log position of its own snapshot (MariaDB takes it from the snapshot, without locking). `binlog.py position` copies it into `…-binlog.json`, part of the set. A set without it can be restored but not brought forward (log: `no point-in-time recovery from this backup`).
2. `restic init` if the repository does not exist yet (log: `initialising repository`). Create the bucket at the storage provider first: restic retries a missing bucket for 15 minutes before it initialises one.
3. `restic backup --host <site> --tag site:<site> --tag ${BACKUP_TAG:-nightly}` of the set. restic encrypts client-side (AES-256) with the password in `RESTIC_PASSWORD_FILE`.
4. `restic forget --host <site> --tag site:<site> --group-by host,tags --prune` with:
   - `--keep-daily ${RESTIC_KEEP_DAILY:-14}`
   - `--keep-weekly ${RESTIC_KEEP_WEEKLY:-8}`
   - `--keep-monthly ${RESTIC_KEEP_MONTHLY:-12}`

   Grouping by tags gives snapshots with another `BACKUP_TAG`, such as `pre-upgrade`, their own retention. The 5-minute binary log and files snapshots (tags `binlog:<site>`, `files:<site>`) keep the last 12 and one a day for 15 days.
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

## Binary log shipping and point-in-time recovery

MariaDB writes every change to its binary log (`mariadb-bin.*`, ROW format, kept 14 days in the
`db-data` volume: [database.md](database.md#binary-logs-and-point-in-time-recovery)). Shipped off
the host and replayed onto the nightly backup, it brings a restore forward to minutes before a
loss.

**Shipping** (`binlog.py ship`, every 5 minutes):

1. writes a heartbeat row (`__hinode_heartbeat` in the site's database): the newest change a
   replay can recover, which the drill reads back to measure the recovery point;
2. closes the current log (`FLUSH BINARY LOGS`) and copies each closed log not yet copied from
   the server (`mariadb-binlog --read-from-remote-server --raw`, as root through an owner-only
   option file) into the mirror `sites/<site>/private/ops/binlog/logs/`;
3. keeps in the mirror the logs from the oldest position of the last three backups (positions
   in `private/ops/binlog/positions/`), so the newest binary log snapshot alone can bring any of
   those backups forward;
4. `restic backup` of the mirror (host `<site>`, tag `binlog:<site>`);
5. `restic backup` of the site's `public/files` and `private/files` (tag `files:<site>`; only new
   files are stored): the logs hold the File records, not the files. Taken after the flush, the
   snapshot holds the file of every record in the logs;
6. `private/ops/binlog/last-shipped.json`: the metric `erp_binlog_last_shipped_timestamp_seconds`.

**Replay** (`ops/backup/replay.sh`, used by `restore.sh` and the drill) onto a set just restored:
it restores the newest binary log snapshot and applies the logs from the set's position
(`mariadb-binlog --start-position … --database <source> --rewrite-db <source>-><target>`, piped to
`mariadb` with `sql_log_bin=0`). Only the source database's changes are applied, under the
target's name; the replay is not logged again. The newest files snapshot is then copied over
the site's files. The logs must run without a gap from the set's position, otherwise the
replay stops with an error and the site keeps the backup's point.

**Limits.**

- A loss of the host loses the changes since the last shipment (normally under 5 minutes).
- Only the last three backups can be brought forward from the newest binary log snapshot. To
  bring an older one forward, restore an older binary log snapshot by hand (the daily ones are
  kept 15 days) and run `replay.sh` from the scheduler container.
- The replay applies whole logs up to `--until` (UTC): it cannot leave out one bad transaction
  in the middle. To recover from a mistake, replay to just before it.
- Turning `BINLOG_SCHEDULE` off (or a server without binary logging) leaves only the nightly
  backups; the alert `ErpBinlogNeverShipped` fires after an hour.

## Weekly restore drill

`ops/backup/restore-drill.sh` proves that the newest backup set can actually be restored and brought forward. It works on the same stack, using a scratch site `restore-drill.invalid` on the same MariaDB, so run it off-peak (the default schedule is Sunday 04:30 JST).

1. Restores the newest backup set of the site (`restic snapshots --host <site> --tag site:<site> --latest 1`) into a temporary directory.
2. Finds the dump and the public and private file archives (`.tar` or `.tgz`). It **stops with an error if the snapshot has no file archives**.
3. Strips trigger `DEFINER` clauses from the dump, so the scratch site's own database user can load it.
4. Creates the scratch site with a random Administrator password.
5. Adopts the source site's encryption key from the snapshot's `*-site_config_backup.json` (`ops/backup/adopt_encryption_key.py`, file to file).
6. Runs `bench restore` with both archives, then replays the shipped binary logs onto the scratch site (`replay.sh`; skipped, and reported, when the set records no position or nothing was shipped since it), then `bench migrate`.
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
   {"snapshot": "…", "source_site": "…", "restore_seconds": 44,
    "pitr": {"replayed": true, "from": "mariadb-bin.000042:73517", "logs": 26, "recovered_to": "…Z", "rpo_seconds": 212.4},
    "checks": {"ok": true, "checks": [ … ]}}
   ```

   `rpo_seconds` is the age of the newest recovered change when the drill began: what a loss of the host at that moment would have lost.

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
./scripts/deploy/restore.sh "$ENV"                 # newest backup set, brought forward to the newest shipped change
./scripts/deploy/restore.sh "$ENV" <snapshot id>   # a specific set (restic snapshots --tag site:<site> shows the ids)
./scripts/deploy/restore.sh "$ENV" --until "2026-10-25 01:40:00"   # forward to just before 01:40 UTC
./scripts/deploy/restore.sh "$ENV" --no-replay     # the set as it is
```

1. Asks you to **type the site name** to confirm. Anything else aborts.
2. `bench --site <site> set-maintenance-mode on`.
3. In `scheduler`:
   - `restic restore` of the snapshot;
   - strip DEFINERs;
   - find the archives (`.tar`/`.tgz`; **fails if none**);
   - `bench --site <site> restore <dump> --with-public-files … --with-private-files … --force`;
   - adopt the backup's encryption key into the site config;
   - unless `--no-replay`: the binary log replay up to `--until` or the newest shipped change, and its report (`recovered_to`).
4. `bench --site <site> migrate`.
5. `bench --site <site> set-maintenance-mode off`, then wait until the site answers *ready* (workers keep the site config for up to 60 seconds).
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

- **If the replay fails** (logs missing between the set and now, a replay error), the site holds the backup set and stays in maintenance mode. Run again with `--no-replay` to accept the backup's point, or with an older `--until`.

**Data written after the newest shipped change is lost** (with `--no-replay`: after the set). Only whole-site restores exist: restoring single documents or files is **not implemented**.

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

Lost: the changes and uploads after the newest shipment (normally under 5 minutes before the loss). Also lost: unprocessed files in the integration file-drop directories, which are not in backups.

## Related

- [upgrade.md](upgrade.md): pre-upgrade backup and rollback
- [database.md](database.md)
- [monitoring.md](monitoring.md)
- [troubleshooting.md](troubleshooting.md#backup-failure-alerts)
