# Troubleshooting

Common failures, how to diagnose them, and the first fix. Commands run from the repository checkout on the host.

```bash
ENV=/etc/hinode/production.env
SITE=erp.hinode.example
```

## First look

```bash
./scripts/deploy/compose.sh "$ENV" ps                                  # state, health, exit codes
./scripts/deploy/smoke.sh "$ENV"                                       # live, ready, portals, login
./scripts/deploy/compose.sh "$ENV" exec -T frontend curl -s -H "Host: $SITE" \
  http://127.0.0.1:8080/api/method/automotive_core.ops.health.ready   # which dependency fails
./scripts/deploy/compose.sh "$ENV" logs --tail 200 <service>           # backend, frontend, websocket, queue-short,
                                                                       # queue-long, scheduler, cron, db, proxy, clamav
```

| Where | What |
|---|---|
| `compose.sh … logs <service>` | container output; Traefik access log on `proxy`; backup and drill runs on `cron` |
| `sites/<site>/logs/` in any app container | Frappe site logs, including `automotive.log` (integration events with correlation ids) |
| Desk **Error Log** (`/desk/error-log`) | server exceptions, failed trigger creation, audit chain failures |
| Desk **RQ Job** / **RQ Worker** / **System Health Report** | job queues, failed jobs, workers |
| `bench doctor`, `bench --site "$SITE" show-pending-jobs` | worker and queue diagnostics (run with `compose.sh … exec -T backend`) |

## Stack does not start

| Symptom | Cause and fix |
|---|---|
| `ERROR: missing env file: …` or `bad key in …` | Path wrong, or a line that is not `KEY=VALUE` with an upper-case key. No quotes and no `export`. |
| `TLS_MODE must be letsencrypt, files or none` | Fix `TLS_MODE` |
| `required variable … is missing a value: set CUSTOM_IMAGE` (or `SITES_RULE`, `RESTIC_REPOSITORY`, `TLS_CERT_DIR`, …) | The variable named in the message is missing from `$ENV` |
| `pin mismatch in …/frappe_docker: expected …, found …` | The cached frappe_docker clone does not match `infra/versions.env`. Remove `${UPSTREAM_DIR:-$HOME/.cache/toyota-erp/upstream}/frappe_docker` and run again (needs GitHub access). |
| Image cannot be pulled | Registry login on the host, the tag in `CUSTOM_TAG`, and `PULL_POLICY` (`never` needs a local image) |
| `configurator` not `Exited (0)` | `logs configurator`: it needs `db` healthy and Redis up |
| `db` unhealthy or restarting | `logs db`. Look for a full disk, an unreadable secret file, or OOM. The root password applies only to a new volume. |
| Containers `Exited (137)` | Killed for memory: raise the service's `*_MEM_LIMIT`. On a dedicated host, check `systemctl show erp.slice -p MemoryHigh -p MemoryMax` (see [install-fresh-host.md](install-fresh-host.md#1-prepare-the-host)). |
| `permission denied` on `/run/secrets/…` or `/etc/prometheus/erp-metrics-token` | Secret file ownership: run `./scripts/deploy/preflight.sh "$ENV"`, then fix as [install-fresh-host.md](install-fresh-host.md#3-provision-secrets) describes |
| Nothing runs after a reboot | Docker not enabled at boot: rerun `install-docker-ubuntu.sh` with `DOCKER_START_AT_BOOT=yes` |

Run `./scripts/deploy/preflight.sh "$ENV"` first; it catches most of these.

## Site does not resolve (404, wrong site, TLS error)

How a request reaches the site:

1. **Traefik** routes only requests matching `SITES_RULE`. Other host names get Traefik's `404 page not found`.
2. **nginx** in `frontend` passes the request host as the site name (`FRAPPE_SITE_NAME_HEADER`, default `$host`).
3. **Frappe** serves the site whose directory name equals that host, which is `SITE_NAME`.

Checks and fixes:

- Use exactly `https://<SITE_NAME>/`. To serve extra host names:
  - extend the rule, e.g. ``SITES_RULE=Host(`erp.hinode.example`) || Host(`erp.internal.example`)``;
  - set `FRAPPE_SITE_NAME_HEADER=<SITE_NAME>` so every host maps to the one site;
  - apply with `compose.sh "$ENV" up -d`.
- **Test without Traefik:**

  ```bash
  ./scripts/deploy/compose.sh "$ENV" exec -T frontend curl -s -o /dev/null -w '%{http_code}\n' \
    -H "Host: $SITE" http://127.0.0.1:8080/api/method/automotive_core.ops.health.live     # 200
  ```

- **TLS handshake errors.** Traefik uses strict SNI, so the certificate must name the host. `preflight.sh` checks the SAN for `TLS_MODE=files`. For Let's Encrypt, port 80 must be reachable from the internet.
- **Site missing.** `new-site.sh` was not run, or failed. Check:

  ```bash
  ./scripts/deploy/compose.sh "$ENV" exec -T backend test -f "sites/$SITE/site_config.json" && echo present
  ```

## 502 Bad Gateway after a restart

nginx in `frontend` resolves `backend` and `websocket` once, when it starts. After those containers are recreated (`up -d` with changed settings, an upgrade or a crash recovery), nginx may keep the old addresses. The CI smoke test restarts both after creating the site for this reason.

```bash
./scripts/deploy/compose.sh "$ENV" restart frontend
```

Other 502 causes:

- **Backend still booting** (gunicorn preloads the app). Wait for `backend` to be healthy.
- **Traefik's own 502.** The `frontend` container is down: check `ps frontend`.

## 503 for everyone

The site is in maintenance mode: a failed `upgrade.sh` or `restore.sh` leaves it on. Finish or roll back the operation ([upgrade.md](upgrade.md#rollback)), then:

```bash
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" set-maintenance-mode off
```

`ready` returning 503 with `"status": "unavailable"` names the failing dependency: `database`, `cache`, `queue` or `scheduler`.

## Migrations failing

`bench migrate` prints the traceback of the failing patch or sync step. Through `upgrade.sh`, the site stays in maintenance mode with the new image running.

1. **Read the error.** Patches are listed in `apps/*/*/patches.txt`. Each runs once and is recorded, so a rerun continues where it failed.
2. **Fix the cause.** Typical causes: data a patch does not expect, or a schema change blocked by existing data or by database limits.
3. **Rerun:**

   ```bash
   ./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" migrate
   ```

4. **If it cannot be fixed quickly,** roll back: previous image plus the pre-upgrade snapshot ([upgrade.md](upgrade.md#rollback)).
5. **Trigger errors.** Errors from the database guards do not fail migrate. They are written to Error Log as `automotive_core: could not create trigger …`; see [database.md](database.md#immutability-triggers).

## Stuck or failed background jobs

- **Is anything running?**
  - `ps queue-short queue-long scheduler redis-queue`;
  - `bench --site "$SITE" scheduler status`;
  - `bench doctor`.
- **What is waiting?**
  - `bench --site "$SITE" show-pending-jobs`;
  - desk **RQ Job**, filtered by status `queued` or `started` and by queue.
- **What failed?**
  - **RQ Job** with status `failed`; *Exc Info* holds the traceback;
  - Error Log;
  - after fixing, clear old failures with *Actions → Remove Failed Jobs*.
- **Self-healing that already exists:**
  - outbound messages stuck in Sending for more than 10 minutes become due again;
  - inbound messages stuck in Received or Processing for more than 5 minutes are re-queued every minute;
  - Planning Runs left Running for more than 2 hours become Failed (hourly job).
- **Restart workers** that hang: `./scripts/deploy/compose.sh "$ENV" restart queue-short queue-long`. Queued jobs survive in `redis-queue`, which uses an append-only file.
- **Scheduler disabled** (for example on a reference stack after the e2e suite): `bench --site "$SITE" enable-scheduler`, or *Enable Scheduler* in the RQ Job list.

## Repost Item Valuation backlog

ERPNext recomputes stock valuation and GL after back-dated or cancelled stock transactions through **Repost Item Valuation** documents (`/desk/repost-item-valuation`). Its own scheduled jobs process them:

- the hourly maintenance job (`repost_entries`);
- or, with *Enable Parallel Reposting* in **Stock Reposting Settings**, every 15 minutes (`run_parallel_reposting`).

**Only inside the configured timeslot:** *Limit Reposting Timeslot*, *Start Time* / *End Time*, *Limits don't apply on*.

**Symptoms of a backlog:**

- stock and GL values lag behind;
- the period-close check `STOCK-GL` fails;
- many Repost Item Valuation records are Queued or In Progress.

**Fix:**

1. Filter the list by status. Open **Failed** entries and read their *Error Log*.
2. Fix the cause, then use **Restart** on the entry. **Start Reposting** triggers processing at once, within the timeslot.
3. Check that the workers and the scheduler are running (above).
4. If reposting is limited to a timeslot, expect the backlog to clear only inside it.

## Planning run Failed

A **Planning Run** (`/desk/planning-run`) with status **Failed** has the traceback in *Error*. If it shows "worker stopped; retry the run", the hourly recovery found it Running for more than 2 hours. `ErpPlanningRunFailed` fires while any run is Failed.

1. Read *Error* and fix the cause. Typical causes: master data, the parameter set, or a timeout (1 hour on the `long` queue).
2. **Retry that run** so its status leaves Failed. `execute` accepts Queued and Failed runs. A run stuck in Queued because a worker was down can be re-queued the same way.

   ```bash
   ./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" execute \
     automotive_manufacturing.production_planning.planning_run.enqueue --kwargs "{'name': '<planning run name>'}"
   ```

3. Planners can also start a new run from the submitted **Planning Parameter Set** (*Run Planning*). The old Failed run then still counts for the alert.

## Inbound messages failing

See [integration-operations.md](integration-operations.md) for statuses and actions.

| Symptom | Cause |
|---|---|
| Partner gets **401** | Unknown partner code, no inbound secret, wrong secret, or nonce reused. Also clock skew beyond *Webhook Tolerance Seconds* (300): check the time sync of both ends. |
| **403** | Partner inactive, or the message type is not in *Allowed Message Types* |
| **409** | Message id reused with a different payload: the partner must use a new id |
| **417** | Envelope or EDI invalid; the response says why |
| **429** | More than *Inbound Rate Limit Per Minute* requests |
| Status **Rejected** | Payload invalid for the handler's model; a carrier reporting a manifest it does not carry, or a supplier invoicing another supplier's receipt; service user not a portal user of the partner; wrong partner or reference; a bank statement that does not add up or names another bank's account ([integration-operations.md](integration-operations.md#bank-statements)) |
| Status **Failed** / **Dead Letter** | The handler raised an error. It is retried with backoff and dead-lettered after *Inbound Max Attempts* (6). Fix the cause, then **Replay**. |
| File-drop file in `rejected/` | Read the `<file>.error.txt` next to it |

## Disk pressure

1. **Find the filesystem.** Run `df -h` on the host. `HostDiskSpaceLow` reports the mount point.
2. **Typical growth**, in the Docker data directory unless you moved it:
   - `db-data`: data plus binary logs (retention `DB_BINLOG_RETENTION_SECONDS`, 14 days);
   - `sites`: attachments; local backup sets, which `bench backup` prunes after `keep_backups_for_hours` (72 h); drill evidence (`private/ops/drills/`, small, not pruned);
   - `prometheus-data`: `PROMETHEUS_RETENTION`;
   - old application images after upgrades. Keep the previous release for rollback and remove older tags with `docker image rm`.
3. **Bounded already:** container logs (20 MB × 5 per container) and Frappe's rotating log files.
4. **Never delete files inside `db-data` by hand.** Shorten the binlog retention, or grow the disk.

## Backup failure alerts

`ErpBackupStale` / `ErpBackupNeverRan` (and `ErpRestoreDrillFailed` for drills): see [backup-restore.md](backup-restore.md#check-backup-health).

```bash
./scripts/deploy/compose.sh "$ENV" logs --tail 200 cron                                # last job output
./scripts/deploy/compose.sh "$ENV" exec -T scheduler bash /opt/hinode/ops/backup/backup.sh   # rerun, see the error
```

| Error | Fix |
|---|---|
| `permission denied` reading `/run/secrets/restic_password` or `db_password` | ownership uid 1000; run preflight |
| restic: wrong password or no key found | `RESTIC_PASSWORD_FILE` does not match the repository; restore the escrowed password |
| S3 access denied, or connection errors | `RESTIC_AWS_*` credentials, bucket policy, or egress to the storage endpoint |
| `ls: cannot access …-database.sql.gz` | `bench backup` failed: disk full, or database access |
| `the snapshot holds no file archives` (restore or drill) | The snapshot was taken without file archives: run a fresh backup |

## Users cannot log in or see data

| Symptom | Cause |
|---|---|
| Asked for an OTP but has no app | Production enforces 2FA for privileged roles: a System Manager uses **Reset OTP Secret** ([access-review.md](access-review.md#account-recovery)) |
| Account disabled | *Access Expires On* passed (daily job) or a Deactivate request; Reactivate via a provisioning request |
| Logged out after 8 hours | Session expiry on production |
| "Segregation of duties: … cannot hold both …" when saving a User | A Block SoD rule; choose other roles |
| Partner sees "This portal is only available to supplier/dealer users." | No **Active** Partner User Access for that user, or the wrong party type |
| Lists empty or a document refused for an internal user | Missing User Permission scope (Company, Plant, …) or plant scope; check the user's User Permissions and Plant Assignments |
| Attachment cannot be opened | Virus Scan Mode Required and the file is not `Clean` ([security-operations.md](security-operations.md#file-uploads-and-clamav)) |

## Scheduled backup or drill never runs

The `cron` service (`ops/backup/cron.py`) logs each job's schedule and next run when it starts, and every run with its exit status. Check `ps cron` and `logs cron`; a wrong `BACKUP_SCHEDULE` or `RESTORE_DRILL_SCHEDULE` (six fields, seconds first, UTC) stops the service at start. After fixing the variable, recreate it:

```bash
./scripts/deploy/compose.sh "$ENV" restart cron
```

Schedules are UTC, with seconds first.

## Related

- [monitoring.md](monitoring.md): alert meanings and first responses
- [upgrade.md](upgrade.md), [backup-restore.md](backup-restore.md), [integration-operations.md](integration-operations.md), [security-operations.md](security-operations.md)
