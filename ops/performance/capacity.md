# Capacity: throughput, slow statements, indexes, queues, growth and storage

Requirement PRF-5. Each section says what is measured, where, and what to change when a limit
is reached. The measurements come from three places:

- **the load test** (`perf.yml`, [README.md](README.md)): per phase, throughput, response
  times, queue lengths, the slow statements, the errors logged and the growth of the database,
  its binary logs and the files;
- **the drill at scale** (`drill.yml`, [backup-restore.md](../runbooks/backup-restore.md)): the
  standard dataset's tables by size, its files, the backup set and what the restic repository
  stores for it;
- **production monitoring** (`MONITORING=1`, [monitoring.md](../runbooks/monitoring.md)): the
  Grafana dashboard *Hinode ERP — Operations* and the alert rules, and
  `scripts/deploy/slow-queries.sh` for the statements.

## Reference measurements

| Measurement | Dataset | Result | Source |
|---|---|---|---|
| Load test, steady 50 users and peak 150 users | standard | pending: the first run on the standard dataset | `perf.yml` |
| Database, files, backup set at the acceptance size | standard | pending: the first `drill.yml` run on the standard dataset | `drill.yml` |
| Trial: load test with 10 users for 3 minutes | closing (3 simulated weeks) | 2.15 requests/s; reads p95 93 ms, writes p95 380 ms; queues empty; no slow statement over 0.5 s; database 143.8 → 144.1 MiB, files 0.9 → 1.3 MiB | `perf.yml` run 40130193627 |
| Trial: drill | closing | database 153 MB (data and indexes), files 3 MB; restored and verified in 49 s, recovery point 5.3 s | `drill.yml` run 40147696835 |

The trials check the method on a small dataset; they size nothing.

## Throughput

**Measured.** Requests per second per phase and per request group (`summary.txt`,
*Throughput*), the busiest second, and the planning run's completion time (`job:Planning Run`)
under load; on the server, `erp_http_request_duration_seconds` by kind (desk, API, portal) and
verb, and `erp_http_responses_total` by status class.

**In production.** Dashboard panels *Requests per second by kind* and *p95 server time by
kind*; alerts `ErpReadLatencyHigh` (p95 of reads over 2 s for 15 minutes), `ErpWriteLatencyHigh`
and `ErpServerErrorRateHigh`.

**When it is not enough.** The backend serves `GUNICORN_WORKERS` × `GUNICORN_THREADS`
requests at once; raise the workers together with `BACKEND_CPUS` and `BACKEND_MEM_LIMIT` (the
production example: 8 workers of 4 threads on 6 CPUs and 6 GB). Beyond one host, the
high-availability option adds application hosts behind a load balancer
([high-availability.md](../runbooks/high-availability.md)). Every database connection counts
against `DB_MAX_CONNECTIONS` (workers × threads, plus the queue workers and the scheduler, on
every application host).

## Slow statements

**Measured.** During each load test phase the database's slow query log records statements
over 0.5 s; `slow-queries.json` keeps their shapes (literals masked), count and time, and the
raw log is deleted.

**In production.** MariaDB keeps statement digests (`--performance-schema=ON` in
`compose.datastores.yaml`): normalised SQL without values, with calls, total, mean and maximum
time, rows examined and sent, and the calls that used no index. There is no slow query log file
to rotate.

```bash
scripts/deploy/slow-queries.sh "$ENV"                      # by total time: the load on the server
scripts/deploy/slow-queries.sh "$ENV" --by mean --limit 30 # by mean time: what users wait for
scripts/deploy/slow-queries.sh "$ENV" --reset              # count from now (a measurement window)
```

`mysqld-exporter` exports the 100 statements with the most time over the last 24 hours, so
Grafana shows *Statements by database time* and *Mean statement time*. The digests hold no
values: they can be shared with whoever tunes the database.

**What to do.** A statement with a high mean time and a non-zero *Without index* count, or
many rows examined per row sent, needs an index: run `EXPLAIN` on it with values from a test
system, then declare the index (below) and measure again.

## Indexes

- **Frappe and ERPNext** index each table's `name` (primary key), `modified`, the `parent` of
  child tables, and the fields their DocTypes declare: ERPNext's ledgers (`tabGL Entry`,
  `tabStock Ledger Entry`) carry their own indexes for voucher, account, item, warehouse and
  posting date.
- **The custom DocTypes** declare theirs in the schema specs (`search_index`, `unique`;
  generated JSON in each app): 60 indexed fields on 36 DocTypes, for the lookups the
  application makes (an Audit Event's chain index, type, reference and correlation id; the
  status and next attempt of outbound messages; a Genealogy Link's serial numbers and batches;
  a Quality Hold's item, batch, serial number and status; the reference of approval requests),
  and unique keys on 18 codes (VIN, plant, line, Kanban card, partner, policy and rule codes,
  idempotency and nonce keys).
- **Custom fields** on standard DocTypes are indexed where the application filters on them:
  the plant of every plant-scoped DocType (`automotive_plant`), the source document and partner
  code of integration-created documents, the supplier lot of a batch.
- **Adding one**: in the spec (`search_index: 1`) for a custom DocType, on the custom field
  (`search_index=1` in the app's `setup/custom_fields.py`), or a composite index in a patch
  (`frappe.db.add_index(doctype, [fields], index_name)`); `bench migrate` creates it. An index
  on a large table is built while `bench migrate` runs, in the maintenance window.

Review the digests after each load test and quarterly in production.

## Queue saturation

**Measured.** The RQ queue lengths sampled every 15 s during each phase (`queues.txt`: maximum
and mean per queue) and the failed jobs before and after (`erp_queue_failed_jobs`); the phases
start once the scheduler has caught up with the jobs overdue since the dataset's last day.

**In production.** `erp_queue_jobs` and `erp_queue_failed_jobs` per queue (dashboard *Queue
depth* and *Failed jobs*); alerts `ErpQueueBacklog` (more than 500 jobs waiting for 15 minutes)
and `ErpFailedJobsGrowing` (more than 20 new failed jobs in an hour); `ErpSchedulerStalled`.

**When a queue keeps a backlog.** One worker serves `short`/`default` (`queue-short`) and
one `long`/`default`/`short` (`queue-long`). `QUEUE_SHORT_WORKERS` and `QUEUE_LONG_WORKERS`
add workers (each a container with `WORKER_MEM_LIMIT` and `WORKER_CPUS`). The long queue
carries planning runs, imports, stock reposting and report preparation, so it saturates first
when several plants plan at once. Each worker holds database connections too
(`DB_MAX_CONNECTIONS`). The queue's Redis persists its jobs (append-only file in the
`redis-queue-data` volume): a backlog of a few thousand jobs is a few megabytes.

## Database growth

**Measured.** Per load test phase, every table's estimated rows and bytes before and after
(`db-*.json`: the largest tables and the most rows added); for the standard dataset (twelve
months of the group's volume, §5 of the brief), every table's size in the drill at scale
(`tables.tsv` in the `drill` artifact).

**In production.** Dashboard panels *Database size* and *Largest tables* (from
`erp_database_size_bytes` and `erp_database_table_size_bytes` of the site's metrics endpoint,
InnoDB's estimates read every 15 minutes); alert `HostDiskSpaceLow`.

**What grows.** The ledgers (GL and stock ledger entries) with every posting; documents and
their child tables; `tabVersion` (the change history of every tracked document) and
`tabComment`; the audit chain (`tabAudit Event`, append-only, never cleared); the integration
inbox and outbox (kept for replay and audit). Frappe's Log Settings clear the logs that only
serve diagnosis: Error Log after 14 days, Activity Log, Route History, Integration Request and
API Request Log after 90 days, Scheduled Job Log after 7. The Access Log (exports, SEC-4) is
not cleared.

## Storage

What each volume holds and how it grows, for sizing a host:

| Volume | Holds | Size |
|---|---|---|
| `db-data` | InnoDB data and indexes, redo log (`--innodb-log-file-size=512M`), binary logs | the database (above) with room for its growth and for rebuilding a large table (about twice the largest table), plus 512 MB, plus the binary logs: 14 days (`DB_BINLOG_RETENTION_SECONDS`) of the write volume, measured per phase in the load test (*Binary logs*: MiB per phase) |
| `sites` | uploaded files, the last three days of local backup sets (`private/backups`), the binary log mirror | files (above) + 3 × the backup set + the binary logs shipped since the last nightly set |
| restic repository (off-host) | nightly sets (14 daily, 8 weekly, 12 monthly), 5-minute binary log and files snapshots | compressed and deduplicated: the drill reports the set's size and what the repository stores |
| `redis-queue-data` | queued jobs (append-only file) | megabytes |
| `prometheus-data` | metrics, 30 days (`PROMETHEUS_RETENTION`), scraped every 30 s | about 1 GB: some 5,000 series (the host, MariaDB with its 100 statement digests, Redis, the site) |

Pending the standard dataset's drill and load test, size the host from the brief's reference
(dedicated database storage) and check `HostDiskSpaceLow` and the *Database size* panel in the
first weeks.
