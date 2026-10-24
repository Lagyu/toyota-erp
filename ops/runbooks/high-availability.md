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
| Singletons | `scheduler`, `cron` | Exactly one active instance, on one application host. If that host fails, start them on another (they hold no state of their own); scheduled jobs missed meanwhile run at their next time |
| Database | MariaDB container | A primary and at least one replica (semi-synchronous replication), with failover by a proxy (e.g. MaxScale) that the application reaches under one name (`DB_HOST`). Operator-provided or a managed MariaDB |
| Cache and queue | Two Redis containers (the queue with AOF) | Redis with Sentinel or a managed Redis, the queue with persistence; `REDIS_CACHE` and `REDIS_QUEUE` point at them |
| Files | the `sites` volume on the host | The `sites` directory on shared storage mounted on every application host (NFS, CephFS or a cloud file service), backed up with the database |
| Backups | nightly restic to S3, weekly drill | Unchanged (from the singleton host), plus replication; binary-log archiving for point-in-time recovery between nightly backups |

**Realtime connections.** The socket.io server keeps a connection per browser tab. With
several application hosts, the load balancer must keep a client on one `websocket` server
(sticky cookie for `/socket.io`) or route `/socket.io` to one host only.

## Configuration on the application hosts

The images, compose files and scripts are the single-host ones; per host:

1. The environment file of [install-fresh-host.md](install-fresh-host.md) with the external
   datastores: `DB_HOST`, `DB_PORT`, `REDIS_CACHE`, `REDIS_QUEUE` (frappe_docker's
   configurator writes them into the common site config).
2. The `sites` volume bound to the shared storage mount, identical on every host.
3. On the hosts that do not carry the singletons, stop `scheduler` and `cron` after `up -d`
   (`compose.sh "$ENV" stop scheduler cron`) and keep them stopped.
4. One `routes.yaml` on the load balancers listing every application host's front end.

**Not packaged yet.** The deploy compose (`scripts/deploy/compose.sh`) always starts the
MariaDB and Redis containers, and has no switch to leave them out or to keep the singletons
on one host: an HA installation edits the compose files for those two points. Packaging
both as settings is the next step of this option.

## Failover and recovery

| Failure | Effect | Recovery | Expected time |
|---|---|---|---|
| One application host | The load balancer stops sending it requests after its health checks fail; open realtime connections reconnect elsewhere | Repair or replace the host, `up -d` | seconds (users), hours (repair) |
| The singleton host | Scheduled jobs (ERPNext's and the backup/drill) pause | Start `scheduler` and `cron` on another host | minutes |
| Database primary | Writes fail until the proxy promotes a replica | Automatic with the proxy; rebuild the old primary as a replica | under a minute to a few minutes |
| Redis queue | Jobs being queued fail until Sentinel promotes a replica | Automatic with Sentinel | seconds |
| Shared storage | Uploads and attachments fail | The storage system's own redundancy | depends on it |
| Total loss (site) | Everything | Restore the nightly backup ([backup-restore.md](backup-restore.md)) on a new stack | the restore drill records the restore time; the 2-hour objective needs a measurement with the production data volume |

## What has been rehearsed

- The single-host topology: install from the runbook, upgrade, simulated reboot, off-host
  backup and restore drill (`rehearsal.yml`; [install-fresh-host.md](install-fresh-host.md)).
- **Not rehearsed:** any part of this page's high-availability option (replication,
  failover, shared storage, several application hosts). Treat it as a design until a
  rehearsal on the target infrastructure records failover times; only then can the
  availability and recovery objectives be stated for it.
