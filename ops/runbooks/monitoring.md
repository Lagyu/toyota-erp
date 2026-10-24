# Monitoring, alerting and logs

This page covers:

- health endpoints and the metrics endpoint;
- the Prometheus, Alertmanager and Grafana overlay;
- **the meaning and first response of every alert** in `infra/deploy/monitoring/alerts.yml`;
- job-queue visibility;
- logs and correlation ids;
- audit chain verification.

Sources:

- `apps/automotive_core/automotive_core/ops/health.py`
- `apps/automotive_core/automotive_core/observability/timing.py` and `observability/context.py`
- `governance/audit.py`
- the app gauges in `automotive_manufacturing/ops_metrics.py` and `automotive_supply_chain/ops_metrics.py`
- `infra/deploy/compose.monitoring.yaml`, `compose.monitoring-proxy.yaml`
- `infra/deploy/monitoring/*`

```bash
ENV=/etc/hinode/production.env
SITE=erp.hinode.example
```

> **Status.** The health endpoints are exercised by every smoke test. The monitoring overlay (`MONITORING=1`) is **not exercised by CI**: the smoke and e2e stacks run with `MONITORING=0`. After enabling it, confirm that every Prometheus target is up and that a test alert reaches your receiver.

## Health endpoints

| Endpoint | Auth | Returns |
|---|---|---|
| `GET /api/method/automotive_core.ops.health.live` | anonymous | `200 {"message": {"status": "ok"}}` (no I/O) |
| `GET /api/method/automotive_core.ops.health.ready` | anonymous | `200` when database, cache, queue and scheduler are fine. `503` with `"status": "unavailable"`, where each failed check's `detail` is the exception class. |
| `GET /api/method/automotive_core.ops.health.metrics` | `Authorization: Bearer <automotive_metrics_token>` | Prometheus text format. `403 {"status": "forbidden"}` without the right token, or when no token is configured. |

- Responses carry `Cache-Control: no-store`.
- The `scheduler` readiness check applies only when `automotive_environment` is `production` or `staging`. It fails when the site's scheduler is disabled or paused.
- While **maintenance mode** is on, every endpoint answers 503.

Docker health checks:

| Service | Check | Timing |
|---|---|---|
| `backend` | `live` on port 8000 | every 30 s, timeout 5 s, 3 retries, 60 s start period |
| `frontend` | `live` through nginx on 8080 | same as backend |
| `websocket` | TCP connect to 9000 | |
| Redis | `redis-cli ping` | every 10 s |
| `db` | `healthcheck.sh --connect --innodb_initialized` | |
| `clamav` | `clamdcheck.sh` | |

Docker marks a failing container *unhealthy* but does not restart it.

Calling the endpoints by hand, through the frontend container as `scripts/deploy/smoke.sh` does:

```bash
./scripts/deploy/compose.sh "$ENV" exec -T frontend curl -s -H "Host: $SITE" \
  http://127.0.0.1:8080/api/method/automotive_core.ops.health.ready
./scripts/deploy/compose.sh "$ENV" exec -T -e TOKEN="$(cat /etc/hinode/secrets/metrics_token)" frontend sh -c \
  'curl -s -H "Host: $SITE_NAME" -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/method/automotive_core.ops.health.metrics'
```

## Metrics

All values are read live from the site. Every series has a `site` label.

**Background work:**

| Metric | Labels | Meaning |
|---|---|---|
| `erp_queue_jobs` | `queue` (default, short, long) | jobs waiting in the RQ queue |
| `erp_queue_failed_jobs` | `queue` | jobs in the RQ failed registry |
| `erp_scheduler_last_job_timestamp_seconds` | | newest `last_execution` of the active Scheduled Job Types. Every run updates it, cron jobs included. |
| `erp_planning_runs_failed` | | Planning Runs with status Failed |

**Integration, governance and errors:**

| Metric | Labels | Meaning |
|---|---|---|
| `erp_outbox_messages` | `status` (Pending, Failed, Dead Letter) | Integration Outbound Messages |
| `erp_inbox_messages` | `status` (Failed, Dead Letter, Rejected) | Integration Inbound Messages |
| `erp_audit_unsealed_events` | | Audit Events not yet sealed into the hash chain |
| `erp_approvals_pending` | | Approval Requests awaiting a decision |
| `erp_error_logs_24h` | | Error Log entries in the last 24 h |

**Backups, drills, scanner:**

| Metric | Labels | Meaning |
|---|---|---|
| `erp_backup_last_success_timestamp_seconds` | | from `sites/<site>/private/ops/backup-last-success.txt`; absent until the first successful backup |
| `erp_restore_drill_last_timestamp_seconds`, `erp_restore_drill_last_ok`, `erp_restore_drill_duration_seconds` | | from `sites/<site>/private/ops/restore-drill-last.json` (written by every drill) |
| `erp_clamav_up` | | 1 if clamd answers PING. Only present when Automotive Settings has a clamd host. |

**Requests:**

| Metric | Labels | Meaning |
|---|---|---|
| `erp_http_request_duration_seconds_bucket` / `_sum` / `_count` | `kind` (api, desk, portal), `verb` (read = GET/HEAD, write = other), `le` | server time histogram |

The request histogram:

- buckets 0.1, 0.25, 0.5, 1, 2, 4, 8, 16 s;
- cumulative counters in the Redis cache, shared by all web workers;
- static assets, `/files/`, `/private/files/`, socket.io and the health endpoints are excluded;
- counters restart from zero when the Redis cache restarts; `rate()` handles that.

**Work-queue gauges:**

| Metric | Meaning |
|---|---|
| `erp_quality_holds_active` | active Quality Holds |
| `erp_andon_open` | Andon Events Raised, Acknowledged or Contained |
| `erp_kanban_signals_overdue` | Kanban Signals Escalated |
| `erp_match_exceptions_open` | open Invoice Match Exceptions |
| `erp_receipt_discrepancies_open` | open Receipt Discrepancies |
| `erp_transport_exceptions_open` | open Transport Exceptions |
| `erp_dealer_orders_to_accept` | Dealer Orders in status Submitted |

## Prometheus, Alertmanager, Grafana (MONITORING=1)

`scripts/deploy/compose.sh` adds `compose.monitoring.yaml` when `MONITORING=1`. It renders `infra/deploy/monitoring/prometheus.yml.tmpl` into `prometheus.rendered.yml`, replacing `${SITE_NAME}`. With a TLS proxy, it also appends a `traefik` scrape job and adds `compose.monitoring-proxy.yaml`.

| Component | Details |
|---|---|
| Prometheus | Scrape and evaluation every 30 s; retention `PROMETHEUS_RETENTION` (30d); volume `prometheus-data`. Networks `backend` and `monitoring`; **not published**. |
| Alertmanager | Config `ALERTMANAGER_CONFIG` (default `infra/deploy/monitoring/alertmanager.yml`); networks `monitoring` and `egress`; not published |
| Grafana | `127.0.0.1:${GRAFANA_PORT:-3000}` only. Admin password from `GRAFANA_ADMIN_PASSWORD_FILE`; sign-up, analytics and update checks disabled. |
| Exporters | `mysqld-exporter` (job `mariadb`, `:9104`), `redis-exporter` (job `redis`, `:9121`, `REDIS_ADDR` lists redis-queue and redis-cache), `node-exporter` (job `node`, `:9100`, host root mounted read-only) |

Scrape jobs:

- **`erp`:** `backend:8000` at `/api/method/automotive_core.ops.health.metrics`, with the bearer token from the mounted `METRICS_TOKEN_FILE` and the header `X-Frappe-Site-Name: <site>`. It bypasses nginx and Traefik.
- **`mariadb`, `redis`, `node`:** the exporters above.
- **`traefik`:** `proxy:8082`, only with `TLS_MODE` letsencrypt or files. This is Traefik's internal metrics entrypoint, which includes certificate expiry.

**Grafana access** through an SSH tunnel from your workstation:

```bash
ssh -L 3000:127.0.0.1:3000 <deploy user>@<erp host>
# then open http://127.0.0.1:3000 and sign in as admin
```

- **Provisioned data source:** *Prometheus* (uid `prometheus`, default).
- **Dashboard:** folder **Hinode ERP**, dashboard **Hinode ERP — Operations** (uid `hinode-erp-ops`, refresh 1 m). Panels:
  - Queue depth
  - Failed jobs
  - Outbox
  - Inbox problems
  - Hours since last backup
  - Unsealed audit events
  - Pending approvals / open work
  - Quality holds and Andon
  - MariaDB connections
  - Redis memory
- **No latency panel yet.** Use *Explore* with:

```text
histogram_quantile(0.95, sum by (le, kind, verb) (rate(erp_http_request_duration_seconds_bucket[10m])))
sum by (kind, verb) (rate(erp_http_request_duration_seconds_count[5m]))
up
```

**After enabling the overlay:**

1. In Explore, `up` must be 1 for every job: `erp`, `mariadb`, `redis`, `node`, and `traefik` if a TLS proxy runs. A token problem on the `erp` job shows as `up == 0` (HTTP 403).
2. Point `ALERTMANAGER_CONFIG` at your own file. Then send a test alert by stopping a non-critical exporter for a few minutes, and confirm it arrives.

## Alertmanager routing

`infra/deploy/monitoring/alertmanager.yml`:

- **Routing:** one receiver `operations`, grouped by `alertname`. Group wait 1 m, group interval 10 m, repeat every 4 h.
- **Critical** alerts repeat every 1 h.
- **The receiver is a placeholder** (`http://127.0.0.1:9/replace-with-operations-webhook`, `send_resolved: true`) and delivers nothing. Before go-live, write an Alertmanager config with your team's channel, keep it outside the repository, and set `ALERTMANAGER_CONFIG=<path>` in `$ENV`. Then run `./scripts/deploy/compose.sh "$ENV" up -d alertmanager`.

## Alert reference

Thresholds are starting points for the 50/150-user profiles; tune them after load measurements. Severity is critical (C) or warning (W).

| Group | Alert | Fires when | Sev |
|---|---|---|---|
| erp-availability | [ErpBackendDown](#erpbackenddown) | `up{job="erp"} == 0` for 5 m | C |
| | [ErpSchedulerStalled](#erpschedulerstalled) | no scheduled job ran for 30 min (`time() - max(erp_scheduler_last_job_timestamp_seconds) > 1800`), for 10 m | C |
| | [MariaDBDown](#mariadbdown) | `mysql_up == 0` for 2 m | C |
| | [RedisDown](#redisdown) | `redis_up == 0` for 2 m | C |
| erp-latency | [ErpReadLatencyHigh](#erpreadlatencyhigh--erpwritelatencyhigh) | p95 server time of reads per `kind` > 2 s (10 m rate), for 15 m | W |
| | [ErpWriteLatencyHigh](#erpreadlatencyhigh--erpwritelatencyhigh) | p95 of writes per `kind` > 4 s, for 15 m | W |
| erp-assurance | [ErpRestoreDrillFailed](#erprestoredrillfailed) | `erp_restore_drill_last_ok == 0` | C |
| | [ErpRestoreDrillStale](#erprestoredrillstale) | last drill older than 8 days, for 1 h | W |
| | [ErpVirusScannerDown](#erpvirusscannerdown) | `erp_clamav_up == 0` for 10 m | C |
| | [ErpCertificateExpiringSoon](#erpcertificateexpiringsoon--erpcertificateexpiring) | a Traefik certificate expires in < 14 days, for 1 h | W |
| | [ErpCertificateExpiring](#erpcertificateexpiringsoon--erpcertificateexpiring) | expires in < 3 days, for 15 m | C |
| erp-work | [ErpPlanningRunFailed](#erpplanningrunfailed) | `erp_planning_runs_failed > 0` for 5 m | W |
| | [ErpQueueBacklog](#erpqueuebacklog) | a queue has > 500 waiting jobs for 15 m | W |
| | [ErpFailedJobsGrowing](#erpfailedjobsgrowing) | > 20 new failed jobs within 1 h | W |
| | [ErpOutboxDeadLetters](#erpoutboxdeadletters--erpinboxfailures) | any outbound message in Dead Letter, for 5 m | W |
| | [ErpInboxFailures](#erpoutboxdeadletters--erpinboxfailures) | any inbound message Failed or Dead Letter, for 15 m | W |
| | [ErpAuditSealingBehind](#erpauditsealingbehind) | > 2000 unsealed audit events for 30 m | W |
| | [ErpErrorLogSpike](#erperrorlogspike) | > 100 Error Log entries in 24 h | W |
| erp-data-protection | [ErpBackupStale](#erpbackupstale--erpbackupneverran) | last successful backup older than 26 h | C |
| | [ErpBackupNeverRan](#erpbackupstale--erpbackupneverran) | no backup marker at all for 30 h | C |
| | [HostDiskSpaceLow](#hostdiskspacelow) | a non-tmpfs/overlay filesystem has < 15% free, for 10 m | W |

General first steps for any alert:

```bash
./scripts/deploy/compose.sh "$ENV" ps                       # state and health of every service
./scripts/deploy/compose.sh "$ENV" logs --tail 200 <service>
./scripts/deploy/smoke.sh "$ENV"
```

### ErpBackendDown

**Meaning.** Prometheus cannot scrape the metrics endpoint on `backend:8000`. Possible causes:

- gunicorn is down or hung;
- the site is in maintenance mode (503);
- the token in site config and `METRICS_TOKEN_FILE` differ (403);
- the site does not resolve.

**First response.**

1. Check `ps backend` and `logs backend`.
2. Run `smoke.sh`: `live` and `ready` tell whether users are affected.
3. If maintenance is on unintentionally, turn it off ([upgrade.md](upgrade.md#maintenance-mode)).
4. If only the metrics scrape fails, compare the token: re-run `new-site.sh` and restart `prometheus`.
5. If gunicorn is hung: `./scripts/deploy/compose.sh "$ENV" restart backend frontend`.

### ErpSchedulerStalled

**Meaning.** No scheduled job has run for over 30 minutes. Normally cron jobs run every minute. Possible causes:

- the `scheduler` container is down;
- the site's scheduler is disabled or paused;
- the workers are down (jobs are enqueued but never executed);
- Redis queue is down;
- maintenance mode is on.

**First response.**

1. Check `ps scheduler queue-short queue-long redis-queue`.
2. Run `bench --site "$SITE" scheduler status`, via `./scripts/deploy/compose.sh "$ENV" exec -T backend ...`.
3. If the scheduler is disabled, run `bench --site "$SITE" enable-scheduler`. On reference stacks, `scripts/ci/e2e.sh` disables it on purpose.
4. Run `bench doctor` for worker diagnostics.
5. Restart `scheduler` and the workers if they are stuck.

### MariaDBDown

**Meaning.** mysqld-exporter cannot reach MariaDB: the database is down, or the exporter's credentials are wrong.

**First response.**

1. Check `ps db` and `logs db`. Look for crashes, a full disk or OOM.
2. Check `ready`: if `database` is ok, the application still reaches it and the exporter account or `MYSQLD_EXPORTER_CNF` is the problem.
3. Never delete files in the `db-data` volume. See [database.md](database.md).

### RedisDown

**Meaning.** redis-exporter cannot reach Redis. `redis-queue` holds the job queues; `redis-cache` holds the cache and the latency counters.

**First response.**

1. Check `ps redis-queue redis-cache`.
2. Run `./scripts/deploy/compose.sh "$ENV" exec -T redis-queue redis-cli ping` (and the same for `redis-cache`). Expect `PONG`.
3. Restart the failed instance. The queue survives restarts (append-only file).
4. If both answer PONG but the alert persists, check the exporter itself. Its `REDIS_ADDR` lists two instances; this overlay is untested in CI.

### ErpReadLatencyHigh / ErpWriteLatencyHigh

**Meaning.** The p95 server time of one request kind exceeds the brief's targets (reads ≤ 2 s, form submissions ≤ 4 s) for 15 minutes. The time is measured inside the application, so network time is not included.

**First response.**

1. Find the affected `kind` (api, desk or portal) in the alert label, and run the Explore queries above.
2. Check for concurrent load:
   - queue backlog;
   - MariaDB connections;
   - host CPU and memory;
   - long reports, planning runs or imports.
3. The Traefik access log (`logs proxy`) shows slow requests by path.
4. Mitigate by adding capacity (`GUNICORN_WORKERS`, `BACKEND_CPUS`, `DB_BUFFER_POOL`; see [upgrade.md](upgrade.md#configuration-changes-without-a-new-image)), or by rescheduling heavy jobs.

Reference performance measurements do not exist yet, so record the occurrence.

### ErpRestoreDrillFailed

**Meaning.** The last weekly restore drill exited with an error. **Backups may not be restorable.**

**First response.**

1. Read the cron log (`logs cron`) and the newest evidence in `sites/<site>/private/ops/drills/`. The evidence names the failed check. See [backup-restore.md](backup-restore.md#check-backup-health).
2. Typical causes:
   - repository access or credentials;
   - the snapshot holds no file archives;
   - the database root password;
   - disk space;
   - a failed integrity check: trial balance, audit chain, attachments or decryption.
3. Fix, then re-run the drill by hand. The alert clears when a drill passes.

### ErpRestoreDrillStale

**Meaning.** No drill has finished for more than 8 days. Possible causes: `cron` is down, or `RESTORE_DRILL_SCHEDULE` is wrong. This alert cannot fire before the very first drill, so run one after installation.

**First response.**

1. Check `ps cron` and `logs cron`: the schedules and next run times at start, then each run with its exit status.
2. Check the schedule value: UTC, seconds first.
3. Run a drill by hand.

### ErpVirusScannerDown

**Meaning.** clamd does not answer PING (checked only when Automotive Settings has a clamd host). Uploads are **not refused**. New files end as `Scan Failed`, and under *Virus Scan Mode = Required* they cannot be downloaded.

**First response.**

1. Check `ps clamav` and `logs clamav`. Typical problems are memory, or signature download through `egress`.
2. Restart `clamav`; allow up to 5 minutes for it to start.
3. Re-scan the files left in `Scan Failed` ([security-operations.md](security-operations.md#file-uploads-and-clamav)).

### ErpCertificateExpiringSoon / ErpCertificateExpiring

**Meaning.** A certificate served by Traefik expires within 14 days (warning) or 3 days (critical). These need `MONITORING=1` and a TLS proxy.

**First response.**

- `TLS_MODE=files`: install the renewed `fullchain.pem` and `privkey.pem` in `TLS_CERT_DIR`, restart `proxy`, then run `preflight.sh`.
- `TLS_MODE=letsencrypt`: check `logs proxy` for ACME errors. Port 80 must be reachable from the internet, and `LETSENCRYPT_EMAIL` must be set.

### ErpPlanningRunFailed

**Meaning.** At least one Planning Run (MRP) has status **Failed**. Its *Error* field holds the traceback. A run left Running for over 2 hours by a dead worker is set to Failed with "worker stopped; retry the run".

**First response.** Open the run and fix the cause, then retry **that run**, so its status leaves Failed and the alert clears ([troubleshooting.md](troubleshooting.md#planning-run-failed)).

### ErpQueueBacklog

**Meaning.** More than 500 jobs have been waiting in one queue for 15 minutes. Possible causes:

- workers are down or saturated;
- a mass operation is running, for example reposting or imports.

**First response.**

1. Check `ps queue-short queue-long`.
2. Run `bench --site "$SITE" show-pending-jobs`.
3. In the desk, check **RQ Job** (`/desk/rq-job`) and **RQ Worker** (`/desk/rq-worker`).
4. Restart stuck workers. Add capacity with `WORKER_CPUS` / `WORKER_MEM_LIMIT` if the load is legitimate.

### ErpFailedJobsGrowing

**Meaning.** More than 20 jobs were added to the RQ failed registry within an hour.

**First response.**

1. Open **RQ Job** with status `failed` and read *Exc Info*. Cross-check the Error Log.
2. Fix the cause.
3. Clear old failures with *Actions → Remove Failed Jobs* in the RQ Job list.

### ErpOutboxDeadLetters / ErpInboxFailures

**Meaning.** Partner messages exhausted their retries (outbound Dead Letter), or inbound messages are failing or dead-lettered.

**First response.** Follow [integration-operations.md](integration-operations.md#operator-actions-replay-and-retry-delivery): read *Last Error*, fix the cause, Replay or Retry Delivery, then run the reconciliation.

### ErpAuditSealingBehind

**Meaning.** More than 2000 audit events have waited over 30 minutes to be sealed into the hash chain. Possible causes:

- the 5-minute sealer job is not running (scheduler or workers);
- Automotive Settings → *Audit Chain Enabled* is off.

**First response.**

1. Check ErpSchedulerStalled and the workers.
2. Seal by hand. Each call seals up to 5000 events and prints the count; repeat until nothing is printed.

   ```bash
   ./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" execute automotive_core.governance.audit.seal_pending
   ```

### ErpErrorLogSpike

**Meaning.** More than 100 Error Log entries in the last 24 hours. The window is rolling, so the alert clears only as entries age out.

**First response.**

1. Open **Error Log** (`/desk/error-log`) and group by method or title.
2. Fix the dominant error, and correlate it with deploys and integrations.

### ErpBackupStale / ErpBackupNeverRan

**Meaning.** No successful off-host backup for 26 hours, or no success marker at all for 30 hours (never ran, or the marker was lost).

**First response.**

1. Read `logs cron` for the failed run.
2. Run a backup by hand and read its error ([backup-restore.md](backup-restore.md#check-backup-health)). Typical causes:
   - repository unreachable or credentials changed;
   - restic password file unreadable (uid 1000);
   - disk full;
   - `bench backup` failed.

### HostDiskSpaceLow

**Meaning.** A host filesystem seen by node-exporter has less than 15% free.

**First response.**

1. Run `df -h` on the host to find the filesystem.
2. Common growth inside Docker's data directory:
   - `db-data`: data and binary logs ([database.md](database.md#binary-logs-and-point-in-time-recovery));
   - `sites`: attachments, local backup sets (pruned after 72 h) and drill evidence;
   - `prometheus-data`;
   - container logs, which are capped per container.
3. Free space or grow the disk. Never delete database files by hand.

See [troubleshooting.md](troubleshooting.md#disk-pressure).

## Job-queue visibility

**Queues and workers:**

- Frappe RQ queues are `short`, `default` and `long`.
- `queue-short` serves short and default.
- `queue-long` serves long, default and short.
- The planning runs (MRP) use `long`, with a 1-hour timeout and progress shown on the run.

**Desk:**

| Page | Shows |
|---|---|
| **RQ Job** (`/desk/rq-job`) | jobs by status; failed jobs with traceback; *Remove Failed Jobs*; scheduler indicator |
| **RQ Worker** (`/desk/rq-worker`) | workers and their state |
| **System Health Report** (`/desk/system-health-report`) | queues, workers, failing jobs and errors |
| **Scheduled Job Log** (`/desk/scheduled-job-log`) | runs of non-cron scheduled jobs |
| **Error Log** (`/desk/error-log`) | errors |

**CLI:**

```bash
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" show-pending-jobs
./scripts/deploy/compose.sh "$ENV" exec -T backend bench doctor
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" scheduler status
```

**Metrics:** `erp_queue_jobs` and `erp_queue_failed_jobs`, shown in the Grafana panels *Queue depth* and *Failed jobs*.

## Logs and correlation ids

**Container logs** use the `local` driver, 20 MB × 5 per container. There is **no central log shipping** (not implemented).

```bash
./scripts/deploy/compose.sh "$ENV" logs --tail 200 backend          # also: frontend, queue-short, queue-long, scheduler, cron, db, proxy
./scripts/deploy/compose.sh "$ENV" logs --since 1h proxy            # Traefik access log
```

**Frappe log files** live inside the containers:

- per container: `/home/frappe/frappe-bench/logs/`;
- per site, shared by all containers through the sites volume: `/home/frappe/frappe-bench/sites/<site>/logs/`.

**Structured log `automotive.log`**, written by `automotive_core.observability.context.log`:

- **Location:** `sites/<site>/logs/automotive.log`, rotated at 50 MB with 10 files kept. A per-container copy is in `logs/automotive.log`.
- **Line format:** `<timestamp> <LEVEL> automotive <JSON>`, where the JSON has `event`, `correlation_id`, `site`, `user` and event fields.
- **Level:** the logger is raised to INFO, so the lines are written in production too.
- **Events written:**
  - `integration.inbound.accepted`;
  - `integration.inbound.signature_rejected` (warning);
  - `integration.inbound.previous_secret_used`: a partner still signs with the secret a rotation replaced;
  - `integration.inbound.processed`, `failed`, `dead_letter`, `rejected`;
  - `integration.outbound.delivered`, `retryable_failure`, `permanent_failure`.

**Correlation ids:**

- **Requests:** every HTTP request gets an id, either the incoming `X-Correlation-ID` header if it matches `^[A-Za-z0-9._:-]{8,64}$`, or a new 32-character hex id. The response carries it in `X-Correlation-ID`.
- **Jobs:** background jobs started for inbound messages inherit it through the job argument `automotive_correlation_id`.
- **Storage:** it is stored on Audit Event, Integration Inbound Message and Integration Outbound Message, and sent on outbound webhooks as `X-Correlation-ID`.
- **No distributed tracing** is implemented beyond these ids.

To trace one business event, take the id from a response header or a message record:

```bash
./scripts/deploy/compose.sh "$ENV" exec -T backend bash -c "grep -h '<correlation id>' sites/$SITE/logs/automotive.log*"
```

Then filter **Audit Event** and the integration message lists by *Correlation ID* in the desk.

## Audit chain verification

How the chain is kept:

- Important transitions are written as **Audit Events** in the same transaction:
  - submit, cancel and amend of governed documents;
  - approvals, delegations and provisioning;
  - SoD warnings;
  - integration dead letters and replays;
  - file scan results;
  - period close.
- **Sealing:** a sealer assigns each event a `chain_index` and `event_hash = sha256(previous_hash + event)`. It runs every 5 minutes and after audited events, under the MariaDB named lock `automotive_audit_seal`, and only while Automotive Settings → *Audit Chain Enabled* is on.
- **Immutability:** sealed rows are protected by database triggers ([database.md](database.md#immutability-triggers)).
- **Daily job** `automotive_core.governance.audit.verify_chain_job`:
  1. seals, then verifies the whole chain;
  2. stores *Audit Chain Verified At* and *Audit Chain Last Result* in Automotive Settings;
  3. on a break, writes an Error Log "Audit chain verification failed".
- The same verification runs in the period-close check `AUDIT-CHAIN` and in every restore drill.

Verify by hand:

```bash
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" execute automotive_core.governance.audit.verify_chain
# {"ok": true, "checked": 12345, "first_break": null, "unsealed": 3}
```

**If `ok` is false,** `first_break` reads "missing chain index N (found M)" or "hash mismatch at chain index N (event <id>)"; Audit Event ids are auto-increment numbers. Treat it as a security incident:

1. Do not modify data.
2. Preserve evidence: take a backup with its own tag, `BACKUP_TAG=incident`.
3. Compare the event with the same chain index in a restored older snapshot, for example with a manual drill.
4. Find out how the row changed. The triggers block ordinary updates, so suspect direct database access.

## Related

- [backup-restore.md](backup-restore.md)
- [integration-operations.md](integration-operations.md)
- [security-operations.md](security-operations.md)
- [troubleshooting.md](troubleshooting.md)
