# High-availability deployment option

The delivered and tested topology is **one host** ([install-fresh-host.md](install-fresh-host.md)):
every service restarts on failure and after a reboot, backups go off the host nightly, and a
weekly drill proves they restore. One host cannot meet a 99.9 % availability objective (a
host failure, kernel update or disk replacement takes the system down until it is repaired
or restored), so **that objective is not claimed for it**.

This page describes the option for when the objective is required: what changes, what the
operator provides, and what has and has not been rehearsed. Recovery objectives from the
brief: RPO ≤ 15 minutes, RTO ≤ 2 hours.

## What fails over, and how

| Layer | Single host (delivered) | High-availability option |
|---|---|---|
| Entry | Traefik on the host (`proxy`) | Two load balancer nodes sharing a virtual IP (VRRP, e.g. keepalived), each running the same Traefik configuration with one server per application host in `routes.yaml`, health-checked on `/api/method/automotive_core.ops.health.ready` |
| Application | `frontend`, `backend`, `websocket`, `queue-short`, `queue-long` | The same services on two or more application hosts, from the same image tag. Stateless: sessions and caches are in Redis and the database |
| Singletons | `scheduler`, `cron` | Exactly one active instance, on one application host. If that host fails, start them on another; scheduled jobs missed meanwhile run at their next time. The binary log mirror (`private/ops/binlog`) is on the shared `sites` storage, so another host continues it |
| Database | MariaDB container | A primary and at least one replica (semi-synchronous replication), with failover by a proxy (e.g. MaxScale) that the application reaches under one name (`DB_HOST`). Operator-provided or a managed MariaDB |
| Cache and queue | Two Redis containers (the queue with AOF) | Redis with Sentinel or a managed Redis, the queue with persistence; `REDIS_CACHE` and `REDIS_QUEUE` point at them |
| Files | the `sites` volume on the host | The `sites` directory on shared storage mounted on every application host (NFS, CephFS or a cloud file service), backed up with the database |
| Backups | nightly restic to S3, binary logs and new files shipped every 5 minutes, weekly drill with point-in-time recovery | Unchanged (from the singleton host), plus replication; with a managed database, its own point-in-time recovery may replace the binary log shipping |

**Realtime connections.** The socket.io server keeps a connection per browser tab. With
several application hosts, the load balancer must keep a client on one `websocket` server
(sticky cookie for `/socket.io`) or route `/socket.io` to one host only.

## Configuration on the application hosts

The images, compose files and scripts are the single-host ones, with two settings of the
environment file ([install-fresh-host.md](install-fresh-host.md)):

| Setting | Single host (default) | Application host of this option |
|---|---|---|
| `DATASTORES` | `bundled`: the stack runs MariaDB and Redis (`infra/deploy/compose.datastores.yaml`) | `external`: they are left out; `DB_HOST`, `DB_PORT` (default 3306), `REDIS_CACHE` and `REDIS_QUEUE` (`host:port`) name the operator's, and frappe_docker's configurator writes them into the common site config. `DB_PASSWORD_SECRETS_FILE` holds that MariaDB's administrator password (site creation, backups, restores) |
| `SINGLETONS` | `1`: the host runs `scheduler` and `cron` | `1` on exactly one host, `0` on the others: their `scheduler` and `cron` have no container |

Per host, besides:

1. The `sites` volume on the shared storage mount, the same on every host (for example a
   compose file of the site's own in `COMPOSE_EXTRA_FILES` declaring `sites` as an external
   volume on the mount).
2. One `routes.yaml` on the load balancers listing every application host's front end, with a
   health check on `/api/method/automotive_core.ops.health.ready` (it checks the database,
   Redis and that the scheduler is enabled, not where the scheduler runs, so every healthy
   host stays in the rotation while the singletons move).
3. Commands that act on the site through the scheduler container (`upgrade.sh`, a backup or
   drill by hand, `restore.sh`) run on the host with `SINGLETONS=1`.

With `DATASTORES=external` the services that use the datastores reach them over the stack's
outbound network (`compose.external-datastores.yaml` puts the realtime server on it too). Keep
the datastores on a private network, or give Redis a password (`REDIS_CACHE` and `REDIS_QUEUE`
take `user:password@host:port`).

`preflight.sh` reports both settings; with `DATASTORES=external` it warns that binary log
shipping needs the external server's binary log (with a managed database, use its own
point-in-time recovery and set `BINLOG_SCHEDULE=off`). With `MONITORING=1`, the Redis
exporter follows `REDIS_CACHE` and `REDIS_QUEUE`; point the host in `mysqld_exporter.cnf` at
the external MariaDB.

## Failover and recovery

| Failure | Effect | Recovery | Expected time |
|---|---|---|---|
| One application host | The load balancer stops sending it requests after its health checks fail; open realtime connections reconnect elsewhere | Repair or replace the host, `up -d` | seconds (users), hours (repair) |
| The singleton host | Scheduled jobs (ERPNext's and the backup/drill) pause | On another host set `SINGLETONS=1` and `compose.sh "$ENV" up -d scheduler cron`. Before the failed host rejoins, set `SINGLETONS=0` there and run `up -d` (its scheduler and cron containers are removed): a host that restarts with its old settings starts them again, and then two cron services run every backup twice and two schedulers can enqueue the same job twice | minutes |
| Database primary | Writes fail until the proxy promotes a replica | Automatic with the proxy; rebuild the old primary as a replica | under a minute to a few minutes |
| Redis queue | Jobs being queued fail until Sentinel promotes a replica | Automatic with Sentinel | seconds |
| Shared storage | Uploads and attachments fail | The storage system's own redundancy | depends on it |
| Total loss (site) | Everything | Restore the nightly backup ([backup-restore.md](backup-restore.md)) on a new stack | the restore drill records the restore time; the 2-hour objective needs a measurement with the production data volume |

## What has been rehearsed

- The single-host topology: install from the runbook, upgrade, reboot and power loss, off-host
  backup and restore drill (`rehearsal.yml`; [install-fresh-host.md](install-fresh-host.md)).
- **The application tier of this option** (`rehearsal.yml` job `ha`,
  `scripts/ci/ha-check.sh`, on one runner, each host a compose project that reaches the others
  only by name and published port, as separate machines would): a datastores host
  (MariaDB and Redis), two application hosts with `DATASTORES=external` sharing the `sites`
  volume, one with `SINGLETONS=1`, and a Traefik load balancer health-checking both. It checks
  that both hosts serve the site, that a change made through one and a file written on one
  are seen on the other, and that one scheduler runs; then the singleton host fails (all its
  containers stop) and the report records how many requests through the balancer failed and
  for how long, the singletons move to the other host, and the failed host returns without
  them.
- **Not rehearsed:** database replication and failover, Redis Sentinel, shared storage on a
  real file service, a load balancer pair with a virtual IP, and any of it on separate
  machines. Treat those as a design until a rehearsal on the target infrastructure records
  their failover times; only then can the availability and recovery objectives be stated for
  this option.
