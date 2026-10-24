# Database (MariaDB)

Configuration of the `db` service, the database guards the application installs, binary logs, and safe database access. `apps/automotive_core/automotive_core/setup/db_guards.py` refers to this page for the binary-log setting its triggers need.

```bash
ENV=/etc/hinode/production.env
SITE=erp.hinode.example
```

## Service

- **Image:** MariaDB 11.8, pinned by digest as `MARIADB_IMAGE` in `infra/versions.env`.
- **Placement:** service `db` on the internal `backend` network (no route out). Data lives in the volume `<project>_db-data` (`/var/lib/mysql`).
- **Root password:** read from `DB_PASSWORD_SECRETS_FILE` through `MYSQL_ROOT_PASSWORD_FILE`. MariaDB uses it **only when the volume is initialised**; see [security-operations.md](security-operations.md#database-root-password) for changing it later.
- **Upgrades:** frappe_docker sets `MARIADB_AUTO_UPGRADE=1`, so the system tables are upgraded when a newer pinned image starts.
- **Health check:** `healthcheck.sh --connect --innodb_initialized` every 5 s.

Server options from `infra/deploy/compose.hinode.yaml`:

| Option | Value | Why |
|---|---|---|
| `--character-set-server` / `--collation-server` | `utf8mb4` / `utf8mb4_unicode_ci` (+ `--skip-character-set-client-handshake`) | Frappe requirement, Japanese text |
| `--innodb-buffer-pool-size` | `${DB_BUFFER_POOL:-2G}` (production example `8G`) | cache size |
| `--innodb-log-file-size` | `512M` | |
| `--max-connections` | `${DB_MAX_CONNECTIONS:-400}` (production example `600`) | gunicorn threads + workers + scheduler |
| `--max-allowed-packet` | `256M` | large documents and restores |
| `--log-bin=mariadb-bin`, `--binlog-format=ROW` | | binary log ([below](#binary-logs-and-point-in-time-recovery)) |
| `--binlog-expire-logs-seconds` | `${DB_BINLOG_RETENTION_SECONDS:-1209600}` (14 days) | binary log retention |
| `--log-bin-trust-function-creators=1` | | with binary logging on, MariaDB requires this for the site user (not SUPER) to create the immutability triggers |
| `mem_limit` / `cpus` | `${DB_MEM_LIMIT:-6g}` / `${DB_CPUS:-4}` (production example `12g` / `6`) | keep the buffer pool well below the memory limit |

**Site database user.** Each site gets its own database and user, created by `bench new-site`.

- The scripts use `--mariadb-user-host-login-scope='%'`, so the user can connect from any container on the `backend` network.
- The credentials (`db_name`, `db_password`) are in `sites/<site>/site_config.json`.

## Database guards

`automotive_core` applies these on every `bench migrate` (`after_migrate` → `db_guards.apply`).

### Immutability triggers

| Trigger | Effect |
|---|---|
| `automotive_audit_event_no_update` | A sealed Audit Event cannot be changed ("Audit Event is sealed and immutable"). The content fields of an unsealed event cannot change either ("Audit Event content is immutable"). |
| `automotive_audit_event_no_delete` | Audit Events cannot be deleted |
| `automotive_approval_decision_no_update` | Approval Decisions are immutable |
| `automotive_approval_decision_no_delete` | Approval Decisions cannot be deleted |

The delete triggers let a session through only if it sets `@automotive_allow_audit_purge = 1`. No procedure or tool in the repository does this: **audit purging and retention are not implemented**, and audit records are kept indefinitely. The audit hash chain stays the detection control if someone disables the triggers ([monitoring.md](monitoring.md#audit-chain-verification)).

If a trigger cannot be created, `migrate` does not fail. Instead an Error Log titled `automotive_core: could not create trigger <name>` is written. The health endpoints do **not** report missing triggers yet, so check after installs, restores and upgrades:

```bash
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" execute automotive_core.setup.db_guards.missing_triggers
# expected: no output (empty list). A JSON list names the missing triggers.
```

To recreate missing triggers, fix the cause, usually a missing `--log-bin-trust-function-creators=1` or privileges, then run `bench --site "$SITE" migrate`.

### Unique keys

| Table | Key | Purpose |
|---|---|---|
| `tabIntegration Inbound Message` | `uniq_partner_message` (partner, message_id) | exactly-once storage of partner messages ([integration-operations.md](integration-operations.md)) |

## Dumps, restores and DEFINERs

Frappe dumps include the immutability triggers with a `DEFINER` naming the source site's database user. A restore by another user, for example a scratch site, a new host or a reference copy, would need SUPER. Therefore:

- `scripts/deploy/restore.sh`, `ops/backup/restore-drill.sh` and `scripts/ci/e2e.sh` remove `/*!50017 DEFINER=… */` clauses before `bench restore`;
- `scripts/lib/strip-definers.sh IN.sql.gz OUT.sql.gz` removes both DEFINER forms (used by `scripts/seed/reset-site.sh`).

The triggers then belong to the restoring user, and `bench migrate` re-applies the guards anyway.

## Binary logs and point-in-time recovery

The binary log (`mariadb-bin.*`, ROW format) is enabled so that changes between nightly backups are recorded. It is kept for `DB_BINLOG_RETENTION_SECONDS` (14 days) **in the `db-data` volume on the same host**.

- **No tooling** copies binary logs off the host or replays them onto a restored dump: point-in-time recovery is **not implemented**. This is why the RPO objective is not met ([backup-restore.md](backup-restore.md#recovery-objectives-and-evidence)).
- **Disk growth.** Binary logs grow with the write volume. Watch the free space of the Docker data filesystem (`HostDiskSpaceLow`), and shorten the retention if needed by changing `DB_BINLOG_RETENTION_SECONDS` and recreating `db` with `compose.sh "$ENV" up -d db`.

## Access for diagnosis

```bash
# SQL shell as the site's own user (Frappe's bench command)
./scripts/deploy/compose.sh "$ENV" exec backend bench --site "$SITE" mariadb
# root shell on the server (prompts for the password in DB_PASSWORD_SECRETS_FILE)
./scripts/deploy/compose.sh "$ENV" exec db mariadb -uroot -p
```

Rules:

- Use read-only queries.
- Never edit ledgers (`tabGL Entry`, `tabStock Ledger Entry`), Audit Events or Approval Decisions by SQL. ERPNext documents must be corrected through cancel and amend, which the application audits.
- The slow query log is **not enabled** by the deployment. Enable it only temporarily while investigating, and note that it writes to the `db-data` volume.

## Monitoring

With `MONITORING=1`:

- `mysqld-exporter` connects with the credentials in `MYSQLD_EXPORTER_CNF` (collector `info_schema.processlist`);
- Prometheus scrapes it as job `mariadb`;
- alert `MariaDBDown` fires on `mysql_up == 0` for 2 minutes;
- Grafana shows *MariaDB connections*.

See [monitoring.md](monitoring.md).

## Related

- [backup-restore.md](backup-restore.md)
- [upgrade.md](upgrade.md#schema-migrations-and-data-backfills)
- [troubleshooting.md](troubleshooting.md)
