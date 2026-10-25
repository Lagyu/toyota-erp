# Install on a fresh host

Deploying a production, staging or reference stack on a new Linux host. Steps:

1. host preparation;
2. repository checkout;
3. secrets;
4. environment file;
5. TLS;
6. preflight;
7. first start;
8. site creation;
9. smoke test;
10. backups, ClamAV and monitoring;
11. reboot persistence.

Sources:

- `scripts/host/install-docker-ubuntu.sh`
- `scripts/deploy/{preflight,compose,new-site,smoke}.sh`
- `infra/deploy/compose.hinode.yaml`, `compose.tls-files.yaml`, `compose.tls-letsencrypt.yaml`, `compose.clamav.yaml`, `compose.monitoring.yaml`
- `config/env/*.env.example`
- the pinned upstream frappe_docker compose files (`infra/versions.env`)

## Prerequisites

- **An Ubuntu host** whose archive ships `docker.io`, `docker-compose-v2` and `docker-buildx`. The install script uses the Ubuntu archive; a Docker Engine already installed from Docker's repository (`docker-ce`, with its Compose and Buildx plugins) is kept and only configured.
- **Sizing.**
  - Reference sizing in the brief: 16 vCPU, 64 GB RAM, SSD/NVMe.
  - The memory limits of the production example add up to roughly 30 GB with monitoring and ClamAV: backend 6g, two workers 2g each, MariaDB 12g, Redis 1.5g, ClamAV 2g, monitoring about 2.3g, small services.
  - Performance: `perf.yml` runs the load tests on the standard dataset. The results, and the hardware they were measured on, are in [ops/performance/README.md](../performance/README.md).
- **A DNS name** for the site (`SITE_NAME`) pointing at the host, with ports 443 and 80 reachable by users. HTTP only redirects, but Let's Encrypt needs port 80 from the internet.
- **Outbound network access** from the host to:
  - GitHub: `compose.sh` clones the pinned frappe_docker once;
  - the container registries;
  - the off-host restic repository (S3-compatible);
  - ClamAV signature updates;
  - Let's Encrypt, if used;
  - allow-listed partner endpoints.
- **A deploy user** that runs all scripts. It needs `sudo`, or membership of the `docker` group; `compose.sh` falls back to `sudo docker` when `docker info` fails. A uid of 1000 simplifies secret ownership (step 3).
- **The application image** pushed to your registry, or built on the host. See [upgrade.md](upgrade.md#build-the-image). Production uses the image built **without** `--reference`.

Conventions used in all runbooks:

```bash
cd /srv/toyota-erp                 # the repository checkout on the host (any stable path)
ENV=/etc/hinode/production.env     # the environment file
SITE=erp.hinode.example            # SITE_NAME from that file
```

## External accounts and credentials

Everything a production deployment needs from outside the repository, in one place (GATE-9).
**This repository configures none of the external accounts**: its CI and rehearsals use stand-ins
(a test certificate authority, a local S3 server, a webhook that goes nowhere), and no partner
is connected. Until an account is configured, the function it serves is absent, as the last
column says.

| What | Needed for | Where it is set | Required | Until configured |
|---|---|---|---|---|
| TLS certificate and key, or a Let's Encrypt e-mail with port 80 open | HTTPS | `TLS_MODE`, `TLS_CERT_DIR` or `LETSENCRYPT_EMAIL` in the environment file ([5. TLS](#5-tls)) | yes | preflight fails |
| S3-compatible bucket and access key | off-host backups and binary log shipping | `RESTIC_REPOSITORY`, `RESTIC_AWS_ACCESS_KEY_ID`, `RESTIC_AWS_SECRET_ACCESS_KEY` | yes | backups stay on the host only; preflight fails |
| Outgoing e-mail account (SMTP host, user, password) | notification e-mails, password resets, the authenticator QR code a privileged user gets at first sign-in | Email Account in the desk, after site creation | yes, before privileged users sign in | e-mails fail visibly (Notification Dispatch: Failed); privileged users cannot enrol their authenticator |
| Alert receiver (the operations team's webhook, mail or chat credentials) | alert delivery | `infra/deploy/monitoring/alertmanager.yml` | with `MONITORING=1` | alerts fire in Prometheus and Alertmanager but reach no one |
| Database monitoring account | MariaDB metrics | `mysqld_exporter.cnf` ([3](#3-provision-secrets)) | with `MONITORING=1` | the `mysql` target is down |
| Container registry login | pulling a private image | `docker login` on the host | if the registry is private | build the image on the host instead |
| Identity provider (OIDC/SAML single sign-on, LDAP) | central sign-in | Social Login Key or LDAP Settings ([security-operations.md](security-operations.md#identity-provider-sso-and-ldap)) | no | local accounts with privileged MFA |
| Partner endpoints: each partner's webhook URL, and its host in the allowlist | outbound messages to partners | Integration Partner, Automotive Settings → *Outbound Host Allowlist* ([integration-operations.md](integration-operations.md)) | per partner | messages wait in the outbox; the partner's *Connection Status* stays Not Configured or Configured (Unverified), never Live before a verified delivery |
| Partner inbound secrets | signed deliveries from partners | generated by *Rotate Inbound Secret* and handed to the partner once | per partner | the partner's deliveries are refused (401) |
| Bank partner (statements) | automatic bank reconciliation | an Integration Partner for the bank ([integration-operations.md](integration-operations.md#bank-statements)) | no | statements are imported by hand |
| ClamAV signature updates | virus scanning | outbound access from the `clamav` container (no credential) | recommended | scanning with stale signatures; under *Required* new files stay unreadable while clamd is down |

The secrets generated on the host (database root, Administrator, restic, metrics token,
Grafana) are in [3. Provision secrets](#3-provision-secrets); the escrow list there covers the
external credentials too.

## 1. Prepare the host

```bash
ERP_SLICE_MEMORY_HIGH=infinity ERP_SLICE_MEMORY_MAX=infinity DOCKER_START_AT_BOOT=yes \
  ./scripts/host/install-docker-ubuntu.sh
```

The script re-runs itself with `sudo` and keeps these variables. It is idempotent: it rewrites only the files it manages and restarts Docker only when one changed.

**Settings:**

| Variable | Default | Production setting |
|---|---|---|
| `ERP_SLICE_MEMORY_HIGH` | `8G` | `infinity` on a dedicated host. Above MemoryHigh the kernel throttles the whole stack. |
| `ERP_SLICE_MEMORY_MAX` | `10G` | `infinity` on a dedicated host (as the script header says). |
| `ERP_SLICE_SWAP_MAX` | `1G` | keep |
| `ERP_SLICE_CPU_WEIGHT` / `ERP_SLICE_IO_WEIGHT` | `20` | keep, or raise on a dedicated host |
| `DOCKER_START_AT_BOOT` | `no` | **`yes`**. With `no`, Docker and the whole ERP stack stay down after a reboot. |

The defaults cap everything Docker starts at 10 GB. They are meant for a host shared with other services, and the production stack does not fit under them.

**What the script does:**

1. Writes `/etc/systemd/system/erp.slice` (memory, CPU and IO accounting and limits).
2. Writes drop-ins `/etc/systemd/system/docker.service.d/10-erp-slice.conf` and `/etc/systemd/system/containerd.service.d/10-erp-slice.conf` (`Slice=erp.slice`).
3. Writes `/etc/docker/daemon.json`: `cgroup-parent` erp.slice, log driver `local` with 20 MB × 5 files, BuildKit.
4. Installs `docker.io docker-compose-v2 docker-buildx` if missing.
5. Enables or disables `containerd.service docker.service docker.socket` at boot.

**Expected output** ends with the Docker engine, Compose and Buildx versions and the slice properties.

**Check:**

```bash
systemctl show erp.slice -p MemoryHigh -p MemoryMax -p CPUWeight -p IOWeight   # infinity / infinity / ...
systemctl is-enabled docker.service                                            # enabled
```

## 2. Check out the repository

The checkout stays on the host and must match the image release:

- the compose project directory is `infra/deploy/`;
- the scheduler bind-mounts `ops/backup/` (backup and drill scripts);
- Traefik mounts `infra/deploy/traefik/`;
- Prometheus mounts `infra/deploy/monitoring/`.

```bash
git clone <repository URL> /srv/toyota-erp
cd /srv/toyota-erp && git checkout <release tag>     # the commit the image was built from
```

On first use, `scripts/deploy/compose.sh` clones frappe_docker at `FRAPPE_DOCKER_REF` into `${UPSTREAM_DIR:-$HOME/.cache/toyota-erp/upstream}/frappe_docker` and verifies the pinned commit (`scripts/lib/verify-checkout.sh`). On a host without GitHub access, pre-populate that directory, or set `UPSTREAM_DIR` in the environment of the deploy user.

## 3. Provision secrets

Secrets are files. The environment file only points at them.

**Ownership rule.** Compose file secrets and the Prometheus token are **bind mounts that keep the host owner and mode**, so the user inside the container must be able to read them. `scripts/deploy/preflight.sh` checks this.

| File (production example path) | Variable | Read by | Ownership / mode |
|---|---|---|---|
| `/etc/hinode/secrets/db_root_password` | `DB_PASSWORD_SECRETS_FILE` | MariaDB at first start (`MYSQL_ROOT_PASSWORD_FILE`); the scheduler for restores and drills (uid **1000**, the `frappe` user of the image); `new-site.sh` on the host | owner uid 1000, `0400`. If the deploy user is not uid 1000: `chown 1000:<deploy group>`, `0440`. |
| `/etc/hinode/secrets/restic_password` | `RESTIC_PASSWORD_FILE` | the scheduler (uid 1000): restic repository key | owner uid 1000, `0400` |
| `/etc/hinode/secrets/admin_password` | `ADMIN_PASSWORD_FILE` | `new-site.sh` (Administrator password at site creation) and `smoke.sh`, on the host | readable by the deploy user, e.g. `0400` |
| `/etc/hinode/secrets/metrics_token` | `METRICS_TOKEN_FILE` | `new-site.sh` (site config `automotive_metrics_token`); Prometheus (uid **65534**) when `MONITORING=1` | `MONITORING=1`: `chown 65534:<deploy group>`, `0440`. Otherwise readable by the deploy user. |
| `/etc/hinode/secrets/grafana_admin_password` | `GRAFANA_ADMIN_PASSWORD_FILE` | Grafana (uid **472**), when `MONITORING=1` | owner uid 472, `0400` |
| `/etc/hinode/secrets/mysqld_exporter.cnf` | `MYSQLD_EXPORTER_CNF` | mysqld-exporter (uid **65534**), when `MONITORING=1` | owner uid 65534, `0400`. A MySQL client option file with the credentials of a monitoring account. **No script creates the account or the file.** |

No secret may be readable by "other" (preflight fails).

Generate random values with the same generator the scripts use (`random_secret` in `scripts/lib/common.sh`):

```bash
sudo install -d -m 0755 /etc/hinode
sudo install -d -m 0750 -g "$(id -gn)" /etc/hinode/secrets      # the deploy user can read, others cannot list
for s in db_root_password admin_password metrics_token restic_password grafana_admin_password; do
  sudo sh -c "umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > /etc/hinode/secrets/$s"
done
cd /etc/hinode/secrets
sudo chown 1000:"$(id -gn)" db_root_password && sudo chmod 0440 db_root_password
sudo chown 1000 restic_password && sudo chmod 0400 restic_password
sudo chown "$(id -un)" admin_password && sudo chmod 0400 admin_password
sudo chown 65534:"$(id -gn)" metrics_token && sudo chmod 0440 metrics_token      # MONITORING=1
sudo chown 472 grafana_admin_password && sudo chmod 0400 grafana_admin_password  # MONITORING=1
cd /srv/toyota-erp
```

With `MONITORING=1`, also create `mysqld_exporter.cnf` with the credentials of a MariaDB monitoring account (`chown 65534`, `chmod 0400`).

**Escrow before go-live.** Store copies of these, off the host, in your password vault:

- the restic password;
- the S3 credentials;
- the database root password;
- the filled-in environment file;
- the TLS private key.

**Without the restic password the off-host backups cannot be restored.** The site's `encryption_key` is included in every backup (see [backup-restore.md](backup-restore.md)).

## 4. Environment file

```bash
sudo cp config/env/production.env.example /etc/hinode/production.env
sudo chown "$(id -un)" /etc/hinode/production.env && sudo chmod 600 /etc/hinode/production.env
```

Use `staging.env.example` (paths under `/etc/hinode-staging/`) or `reference.env.example` (paths under `/etc/hinode-reference/`) for those environments. The file name does not matter: every script takes the path as its first argument.

**Format** (`load_env_file` in `scripts/lib/common.sh`):

- one `KEY=VALUE` per line;
- **no quotes and no shell expansion**: everything after the first `=` is the value;
- avoid `$` in values;
- never commit filled-in copies.

The file holds the S3 credentials, so keep it at mode `600`.

Edit at least these:

| Variable | Notes |
|---|---|
| `COMPOSE_PROJECT_NAME` | `hinode` (production); prefixes container, network and volume names |
| `ENVIRONMENT` | `production`, `staging` or `reference` (written to site config `automotive_environment`; production enables 2FA, reference enables the generator) |
| `SITE_NAME`, `SITES_RULE` | the DNS name, and the Traefik rule ``Host(`<name>`)`` |
| `TLS_MODE`, `TLS_CERT_DIR` / `LETSENCRYPT_EMAIL` | see step 5 |
| `CUSTOM_IMAGE`, `CUSTOM_TAG`, `PULL_POLICY` | the immutable image, e.g. `registry.hinode.example/hinode/erp` and `0.1.0-<git sha>`. Never `latest`. `PULL_POLICY=always` pulls on `up`; `never` for locally built images. |
| `*_FILE`, `MYSQLD_EXPORTER_CNF` | the secret files of step 3 |
| `RESTIC_REPOSITORY`, `RESTIC_AWS_ACCESS_KEY_ID`, `RESTIC_AWS_SECRET_ACCESS_KEY` | off-host S3-compatible repository. Only these two AWS variables reach the scheduler, so other restic backends would need their variables added to `compose.hinode.yaml` (not implemented). |
| `BACKUP_SCHEDULE`, `RESTORE_DRILL_SCHEDULE` | Cron with **seconds first, in UTC** (`ops/backup/cron.py` evaluates them in UTC). Defaults: `0 30 17 * * *` = 02:30 JST daily; `0 30 19 * * 6` = Sunday 04:30 JST. |
| `MONITORING` | `1` deploys Prometheus, Alertmanager, Grafana and exporters |
| `CLAMAV`, `VIRUS_SCAN_MODE` | `CLAMAV=1` (the default) deploys ClamAV, and `new-site.sh` then sets *Virus Scan Mode* to `VIRUS_SCAN_MODE` (`Required`, the default, or `Best Effort`) and the clamd host. `CLAMAV=0` leaves ClamAV out and the scanning settings as they are. |
| `DATASTORES`, `SINGLETONS` | Leave out (defaults `bundled`, `1`) on a single host. The high-availability option's application hosts set `DATASTORES=external` with `DB_HOST`, `DB_PORT`, `REDIS_CACHE`, `REDIS_QUEUE`, and `SINGLETONS=0` on all but one ([high-availability.md](high-availability.md)). |

Optional tuning variables and their defaults are listed in the [appendix](#appendix-tuning-variables).

## 5. TLS

| `TLS_MODE` | Compose file | What you provide |
|---|---|---|
| `files` (default of the examples) | `compose.tls-files.yaml` | `fullchain.pem` and `privkey.pem` in `TLS_CERT_DIR`, for example `/etc/hinode/tls`. Mounted read-only into Traefik at `/certs`; certificate config in `infra/deploy/traefik/certificates.yaml`. |
| `letsencrypt` | `compose.tls-letsencrypt.yaml` | `LETSENCRYPT_EMAIL`, and ports 80/443 reachable from the internet (HTTP-01 challenge). Certificates are kept in the `letsencrypt` volume (`/letsencrypt/acme.json`). |
| `none` | frappe_docker `compose.noproxy.yaml` | Plain HTTP on `HTTP_PUBLISH_PORT` (default 8080). For local test stacks only; preflight fails it for production. |

Both TLS modes run the same pinned Traefik (`TRAEFIK_IMAGE`) on the `edge` network with the shared `infra/deploy/traefik/dynamic.yaml`, which provides:

- **TLS:** TLS 1.2 minimum, strict SNI, ECDHE AES-GCM and ChaCha20 suites only;
- **redirect:** HTTP is redirected to HTTPS;
- **headers** (`hinode-headers` middleware):
  - HSTS one year including subdomains;
  - `nosniff`;
  - `X-Frame-Options: SAMEORIGIN`;
  - `Referrer-Policy: strict-origin-when-cross-origin`;
  - a Permissions-Policy that disables camera, microphone and geolocation.

The proxy also answers the site's name inside the stack (an alias on the `egress` network): PDF rendering in the backend fetches the print styles from the site's own URL. With certificates from an internal CA, the application image must trust that CA as well, or PDFs fail. `TLS_MODE=files` is exercised by the production rehearsal (`rehearsal.yml`); `TLS_MODE=letsencrypt` by its `acme` job against Let's Encrypt's ACME test server (Pebble, `scripts/ci/acme-check.sh`: certificate through the HTTP-01 challenge, redirect, HTTPS chain). With the real Let's Encrypt, the name must resolve publicly and ports 80/443 must be reachable: verify it after installation (step 9), or first against the staging directory (`ACME_CA_SERVER`).

## 6. Preflight

```bash
./scripts/deploy/preflight.sh "$ENV"
```

**Static checks only:** nothing is started or changed.

- required variables are set and `ENVIRONMENT` is valid;
- each secret file exists, is not empty, is not world-readable, and is readable by the uid that uses it (step 3);
- for `files`: the certificate pair is present, valid for 30 more days, and has `SITE_NAME` in its SAN;
- for `letsencrypt`: `LETSENCRYPT_EMAIL` is set;
- `none` is refused for production;
- the restic repository settings: S3 needs both credentials; a repository inside the sites volume is refused for production;
- the image is present locally (or will be pulled);
- the merged compose configuration renders.

Rendering the configuration also clones frappe_docker on first use, and writes `infra/deploy/monitoring/prometheus.rendered.yml` when `MONITORING=1`.

**Expected:** `PASS` lines, then `0 failed, N warnings`. Exit code 1 if any check fails. Read every `WARN`.

## 7. First start

```bash
./scripts/deploy/compose.sh "$ENV" pull          # optional with PULL_POLICY=always; needs registry login if private
./scripts/deploy/compose.sh "$ENV" up -d
until ./scripts/deploy/compose.sh "$ENV" exec -T db healthcheck.sh --connect >/dev/null 2>&1; do sleep 5; done
./scripts/deploy/compose.sh "$ENV" ps
```

`compose.sh` assembles these compose files:

- frappe_docker's `compose.yaml`, `compose.mariadb.yaml`, `compose.mariadb-secrets.yaml` and `compose.redis.yaml`;
- the TLS file for `TLS_MODE`;
- `compose.hinode.yaml`;
- `compose.clamav.yaml` when `CLAMAV=1`;
- `compose.monitoring.yaml` when `MONITORING=1`;
- `compose.monitoring-proxy.yaml` when `MONITORING=1` and a TLS proxy runs. This puts Traefik on the `monitoring` network so Prometheus can scrape its metrics entrypoint `:8082`.

Review the merged result with `./scripts/deploy/compose.sh "$ENV" config`.

**Services:**

| Service | Role |
|---|---|
| `configurator` | One-shot: writes the common site config. Expected `Exited (0)`. |
| `backend` | gunicorn |
| `frontend` | nginx |
| `websocket` | socket.io |
| `queue-short` | worker for queues `short,default` |
| `queue-long` | worker for queues `long,default,short` |
| `scheduler` | `bench schedule`; also runs backup and drill jobs |
| `cron` | Runs the nightly backup and the weekly restore drill on their schedules (`ops/backup/cron.py`), with the scheduler's environment; no Docker socket |
| `db` | MariaDB 11.8 |
| `redis-cache` | no persistence |
| `redis-queue` | append-only file, fsync every second |
| `proxy` | Traefik, in the TLS modes |
| `clamav` | when `CLAMAV=1` |
| `prometheus`, `alertmanager`, `grafana`, `mysqld-exporter`, `redis-exporter`, `node-exporter` | when `MONITORING=1` |

**Networks:**

- `edge`: proxy and frontend.
- `backend`: **internal**, with no route out. Database, Redis and all application services.
- `egress`: outbound access for the application services, ClamAV and Alertmanager.
- `monitoring`.

**Volumes** (prefixed with the project name, e.g. `hinode_sites`): `sites`, `db-data`, `redis-queue-data`, `letsencrypt`, `clamav-db`, `prometheus-data`, `alertmanager-data`, `grafana-data`.

**Container hardening:** application containers run with `no-new-privileges`, all capabilities dropped, a 512 MB `/tmp` tmpfs and `init`. Logs use the `local` driver (20 MB × 5).

Before the site exists, the `backend` and `frontend` health checks fail; this is expected until step 8.

## 8. Create the site

```bash
./scripts/deploy/new-site.sh "$ENV"
./scripts/deploy/compose.sh "$ENV" restart backend frontend      # as the CI smoke test does after site creation
```

`new-site.sh` is idempotent: an existing site is only re-configured. It:

1. Creates the site if `sites/<site>/site_config.json` does not exist: `bench new-site` with ERPNext, the Administrator password from `ADMIN_PASSWORD_FILE` and DB user host scope `%`.
2. Installs `automotive_core`, `automotive_manufacturing`, `automotive_supply_chain` and `automotive_portals`. Only for `ENVIRONMENT=reference`, it also installs `automotive_seed` and sets `automotive_allow_seed` 1.
3. Sets site config:
   - `automotive_environment`;
   - `keep_backups_for_hours` = 72, so local backup files are kept three days;
   - `host_name` = `https://<site>`;
   - `automotive_metrics_token`.
4. Applies the tenant: `automotive_core.setup.tenant.apply` with `hinode`. This creates:
   - companies, fiscal years and holiday calendars;
   - plants, warehouses, lines and shifts;
   - intercompany parties;
   - SoD rules and approval policies;
   - system settings, plus 2FA, session expiry and login restrictions on production.

   It logs the number of changes.
5. Enables the scheduler, and runs `bench use <site>`.

**Expected:** the last line is `... [new-site] site <site> ready (<environment>)`.

## 9. Smoke test

```bash
./scripts/deploy/smoke.sh "$ENV"
```

It runs through the frontend container with the site's Host header. Expected output, exit code 0:

```text
... [smoke] ok   live (200)
... [smoke] ok   ready (200)
... [smoke] ok   metrics-needs-token (403)
... [smoke] ok   login-page (200)
... [smoke] ok   supplier-portal-guest (302)
... [smoke] ok   dealer-portal-guest (302)
... [smoke] ok   desk-guest (301)            # 301 or 302
... [smoke] ok   administrator login
```

A `FAIL` line names the check and the HTTP code; see [troubleshooting.md](troubleshooting.md). Then check from a client through TLS:

```bash
curl -sS -o /dev/null -D - "https://$SITE/api/method/automotive_core.ops.health.live"
# a GET that prints the response headers (the endpoint accepts GET only, so no -I/HEAD)
# expect status 200 and a strict-transport-security header; http:// must redirect to https://
```

Keep the output of both as installation evidence (brief §8.2 gate 6). No script stores it for you.

## 10. Enable backups, ClamAV and monitoring

- **Backups** run automatically from the first start: the `cron` service runs `ops/backup/backup.sh` nightly and `ops/backup/restore-drill.sh` weekly. Take a first backup and drill now, and confirm the evidence, as described in [backup-restore.md](backup-restore.md):

  ```bash
  ./scripts/deploy/compose.sh "$ENV" exec -T scheduler bash /opt/hinode/ops/backup/backup.sh
  ./scripts/deploy/compose.sh "$ENV" exec -T scheduler bash /opt/hinode/ops/backup/restore-drill.sh
  ```

- **ClamAV** (`CLAMAV=1`). The `clamav` container needs about 2 GB RAM and up to 5 minutes for its first signature download (health check `clamdcheck.sh`, start period 300 s). `new-site.sh` has set *Virus Scan Mode* (`VIRUS_SCAN_MODE`, Required by default) and the clamd host (`clamav:3310`) in Automotive Settings. Check it once clamd is up:
  1. Upload a test attachment to any document. Its File record must show *Malware Scan* = **Clean** within a minute.
  2. Files uploaded while clamd was still loading show `Scan Failed`; the hourly re-scan takes care of them.

  See [security-operations.md](security-operations.md#file-uploads-and-clamav).
- **Monitoring** (`MONITORING=1`):
  1. Point `ALERTMANAGER_CONFIG` at an Alertmanager file with your team's receiver. The default sends to a placeholder URL.
  2. Open Grafana through an SSH tunnel.

  See [monitoring.md](monitoring.md). The production rehearsal (`rehearsal.yml`) runs the overlay and checks that every Prometheus target is up; check them on your host too after enabling it.
- **Outgoing email.** Configure an Email Account in the desk for welcome mails, password resets and notifications. No script does this. On production it is needed before any privileged user (two-factor roles, IT Operator included) logs in for the first time: Frappe e-mails them the link to their authenticator QR code, and their login fails until it can ([security-operations.md](security-operations.md#privileged-mfa)).
- **Administrator and users.**
  - On production, privileged roles (including System Manager, held by Administrator) must use an OTP app. Enrol at the first login.
  - Then provision users as in [access-review.md](access-review.md).

## 11. Reboot persistence

**What restarts by itself:**

- Docker starts at boot if `DOCKER_START_AT_BOOT=yes` was used in step 1.
- Every service has a restart policy:
  - frappe_docker services: `unless-stopped` via `RESTART_POLICY`;
  - `configurator`: `on-failure`;
  - db, Redis, proxy, cron, ClamAV and the monitoring services: `unless-stopped`.
- Queued background jobs survive in `redis-queue`, which uses an append-only file.

Rehearse a reboot before go-live, then check (CI does both, a reboot and a power loss, on a
fresh VM installed by this runbook: the `reboot` job of `rehearsal.yml`, `scripts/ci/reboot-check.sh`):

```bash
systemctl is-active docker                                                   # active
./scripts/deploy/compose.sh "$ENV" ps                                        # all Up / healthy, configurator Exited (0)
./scripts/deploy/smoke.sh "$ENV"                                             # all ok
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" scheduler status   # Scheduler is enabled ...
./scripts/deploy/compose.sh "$ENV" logs --tail 20 cron                       # the backup and restore-drill schedules and next runs
```

If the site answers 502 after the reboot, restart the frontend ([troubleshooting.md](troubleshooting.md#502-bad-gateway-after-a-restart)).

## Failure handling

- **`preflight.sh` fails:** fix each `FAIL` line and rerun.
- **`new-site.sh` stops half-way:** read its output, fix the cause, rerun. It is idempotent.
- **Starting over before go-live:** destroy the stack and all its data, then repeat from step 7:

  ```bash
  ./scripts/deploy/compose.sh "$ENV" down --volumes --remove-orphans
  ```

## Appendix: tuning variables

Optional variables, read from the environment file by the compose files.

**Application services:**

| Variable | Default | Used for |
|---|---|---|
| `GUNICORN_WORKERS`, `GUNICORN_THREADS`, `GUNICORN_TIMEOUT` | 4, 4, 120 | backend |
| `BACKEND_MEM_LIMIT`, `BACKEND_CPUS` | 3g, 4 | backend |
| `FRONTEND_MEM_LIMIT`, `FRONTEND_CPUS` | 256m, 1 | frontend |
| `WEBSOCKET_MEM_LIMIT` | 512m | websocket |
| `WORKER_MEM_LIMIT`, `WORKER_CPUS` | 2g, 2 | queue-short, queue-long |

**Database and Redis:**

| Variable | Default | Used for |
|---|---|---|
| `DB_BUFFER_POOL`, `DB_MAX_CONNECTIONS` | 2G, 400 | MariaDB |
| `DB_BINLOG_RETENTION_SECONDS` | 1209600 (14 days) | MariaDB binary logs |
| `DB_MEM_LIMIT`, `DB_CPUS` | 6g, 4 | MariaDB |
| `REDIS_CACHE_MAXMEMORY` | 768mb | redis-cache (allkeys-lru) |

**Frontend (nginx):**

| Variable | Default | Used for |
|---|---|---|
| `FRAPPE_SITE_NAME_HEADER` | `$host` | site resolution |
| `UPSTREAM_REAL_IP_ADDRESS` | `0.0.0.0/0` | trusted proxy range for `X-Forwarded-For` |
| `PROXY_READ_TIMEOUT` | 120 | nginx |
| `CLIENT_MAX_BODY_SIZE` | 25m | upload size limit |
| `HTTP_PUBLISH_PORT`, `HTTPS_PUBLISH_PORT` | 80, 443 (8080 with `TLS_MODE=none`) | published ports; may include a bind address, e.g. `127.0.0.1:18443` |

**Backups:**

| Variable | Default | Used for |
|---|---|---|
| `RESTIC_KEEP_DAILY`, `RESTIC_KEEP_WEEKLY`, `RESTIC_KEEP_MONTHLY` | 14, 8, 12 | backup retention |

**ClamAV, monitoring and compose:**

| Variable | Default | Used for |
|---|---|---|
| `CLAMAV_MEM_LIMIT` | 2g | ClamAV |
| `PROMETHEUS_RETENTION` | 30d | Prometheus |
| `GRAFANA_PORT` | 3000 (bound to 127.0.0.1) | Grafana |
| `ALERTMANAGER_CONFIG` | `./monitoring/alertmanager.yml` | Alertmanager routing file |
| `RESTART_POLICY` | `unless-stopped` | frappe_docker application services |
| `UPSTREAM_DIR` | `$HOME/.cache/toyota-erp/upstream` | where `compose.sh` keeps frappe_docker |
| `ACME_CA_SERVER` | Let's Encrypt's production directory | `TLS_MODE=letsencrypt`: another ACME directory, e.g. Let's Encrypt's staging one (`https://acme-staging-v02.api.letsencrypt.org/directory`) for a trial |
| `COMPOSE_EXTRA_FILES` | none | compose files of the site's own (absolute paths, space separated), merged after the Hinode ones |

## Related

- [upgrade.md](upgrade.md): image builds, upgrades, rollback
- [backup-restore.md](backup-restore.md)
- [monitoring.md](monitoring.md)
- [security-operations.md](security-operations.md)
- [troubleshooting.md](troubleshooting.md)
