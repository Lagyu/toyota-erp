# Security operations

Secrets and their rotation, privileged MFA, the dependency audit and its allowlist, SBOM and image scanning, file-upload controls and ClamAV, the outbound allowlist, TLS and headers, and least-privilege notes.

```bash
ENV=/etc/hinode/production.env
SITE=erp.hinode.example
```

## Secrets

### Where secrets live

| Secret | Location | Used by |
|---|---|---|
| Database root password | `DB_PASSWORD_SECRETS_FILE` → compose secret `db_password` | MariaDB volume initialisation; scheduler and cron (drill, restore); `new-site.sh` |
| Administrator password | `ADMIN_PASSWORD_FILE` | `new-site.sh` at site creation; `smoke.sh` login check |
| Metrics token | `METRICS_TOKEN_FILE` → site config `automotive_metrics_token` | metrics endpoint (header `X-Hinode-Metrics-Token`); Prometheus |
| Restic password | `RESTIC_PASSWORD_FILE` → compose secret `restic_password` | encryption key of the backup repository (scheduler and cron) |
| S3 credentials | `RESTIC_AWS_ACCESS_KEY_ID` / `RESTIC_AWS_SECRET_ACCESS_KEY` **in the environment file** | scheduler and cron (backup, restore) |
| Grafana admin password | `GRAFANA_ADMIN_PASSWORD_FILE` | Grafana (`MONITORING=1`) |
| Exporter DB credentials | `MYSQLD_EXPORTER_CNF` | mysqld-exporter (`MONITORING=1`) |
| TLS private key | `TLS_CERT_DIR/privkey.pem` (or the `letsencrypt` volume) | Traefik |
| Site DB password, `encryption_key` | `sites/<site>/site_config.json` (sites volume) | Frappe |
| Encrypted application secrets: partner signing secrets, user API secrets, email account passwords, OTP secrets | database, encrypted with the site's `encryption_key` | Frappe |

**Handling rules:**

- Secret files are owned and moded so that only the reading container user and the deploy user can read them; [install-fresh-host.md](install-fresh-host.md#3-provision-secrets) has the table. `./scripts/deploy/preflight.sh "$ENV"` verifies this before every deploy.
- The environment file holds the S3 credentials: keep it at mode `600`. `compose.sh` passes its values to Compose through a temporary owner-only file that it deletes on exit.
- **Container environments are not secret from Docker users.** Values passed to containers (S3 keys, values given with `exec -e`) are visible to anyone who can use Docker on the host. Membership of the `docker` group is equivalent to root: keep it to the deploy user.
- **Backups contain secrets.** Every backup set contains `*-site_config_backup.json` with the `encryption_key` and DB password. The restic repository is encrypted, so its password is the master secret: escrow it off the host.
- **Never change `encryption_key`.** Existing encrypted values would no longer decrypt. Restores carry the backup's key over (`ops/backup/adopt_encryption_key.py`). `new-site.sh` creates the key with the site, and `upgrade.sh` adds one to a site that has none: Frappe would otherwise create it at the first encrypted field, and two processes doing that at once each write their own, losing what the first encrypted.
- Secret files are bind mounts. After **replacing** a file (new inode), restart the containers that read it, because a running container keeps the old file. Rewriting the file in place keeps the inode.

### Rotation

No secret is rotated automatically. Procedures:

#### Metrics token

1. Write a new random value into `METRICS_TOKEN_FILE`, keeping owner and mode (with `MONITORING=1`, `65534:<deploy group>` and `0440`).
2. Apply it to the site. `new-site.sh` is idempotent and sets the token from the file:

   ```bash
   ./scripts/deploy/new-site.sh "$ENV"
   ./scripts/deploy/compose.sh "$ENV" restart prometheus      # MONITORING=1
   ```

3. Verify: `smoke.sh` shows `metrics-needs-token (403)`, and the Prometheus target `erp` is up.

#### Administrator password

```bash
./scripts/deploy/compose.sh "$ENV" exec backend bench --site "$SITE" set-admin-password --logout-all-sessions
```

The command prompts for the password, so it never appears on a command line. Then put the same value into `ADMIN_PASSWORD_FILE`, which `smoke.sh` uses for its login check.

#### Database root password

`MYSQL_ROOT_PASSWORD_FILE` is read only when the volume is first initialised, so changing the file alone changes nothing.

1. Open a root shell: `./scripts/deploy/compose.sh "$ENV" exec db mariadb -uroot -p`.
2. Run `ALTER USER` for each root account. List them with `SELECT user, host FROM mysql.user WHERE user = 'root';`.
3. Write the new value into `DB_PASSWORD_SECRETS_FILE` and restart `scheduler` and `cron`.
4. Verify with a manual restore drill ([backup-restore.md](backup-restore.md#weekly-restore-drill)).

Rotating a site's own database password is **not scripted**.

#### Restic password

restic supports several keys per repository.

1. Stream the new password into the scheduler's tmpfs:

   ```bash
   ./scripts/deploy/compose.sh "$ENV" exec -T scheduler bash -c 'cat > /tmp/new-restic-password' < new-restic-password
   ```

2. Add it as a key, then list the keys:

   ```bash
   ./scripts/deploy/compose.sh "$ENV" exec -T scheduler restic key add --new-password-file /tmp/new-restic-password
   ./scripts/deploy/compose.sh "$ENV" exec -T scheduler restic key list
   ```

3. Put the new password into `RESTIC_PASSWORD_FILE`, restart `scheduler` and `cron`, and check that `restic snapshots` works.
4. **Escrow the new password.**
5. Remove the old key with `restic key remove <old key id>`, and delete `/tmp/new-restic-password` in the container.

#### Other secrets

- **S3 credentials:** create new keys at the storage provider and update `RESTIC_AWS_*` in `$ENV`. Run `./scripts/deploy/compose.sh "$ENV" up -d scheduler cron` (the environment changed, so they are recreated). Run a manual backup, then revoke the old keys.
- **Grafana admin password:** Grafana applies `GF_SECURITY_ADMIN_PASSWORD__FILE` when it creates its admin user. Change the password in Grafana itself, then update the file so a rebuilt monitoring stack gets the same value.
- **TLS certificate (`TLS_MODE=files`):**
  - Replace `fullchain.pem` and `privkey.pem` in `TLS_CERT_DIR`, then `./scripts/deploy/compose.sh "$ENV" restart proxy`.
  - Expiry warnings:
    - `preflight.sh` warns 30 days before expiry.
    - With `MONITORING=1`, Prometheus scrapes Traefik's certificate metrics and alerts at 14 days (`ErpCertificateExpiringSoon`) and 3 days (`ErpCertificateExpiring`).
  - Let's Encrypt certificates are renewed by Traefik.
- **Partner signing secrets:** inbound secrets rotate with an overlap (*Rotate Inbound Secret*); see [integration-operations.md](integration-operations.md#rotating-partner-secrets).
- **User API keys:**
  - Users get keys on their User form (API Access).
  - Deactivation removes them ([access-review.md](access-review.md#deprovisioning)).
  - Revoke a single key by regenerating it.

## Privileged MFA

- **Flagged roles.** On every migrate, `automotive_core` flags these roles with *Two Factor Authentication* (`setup/roles.py`, `PRIVILEGED_ROLES`):
  - System Manager
  - IT Operator
  - Treasury Approver
  - Group Controller
  - Automotive Executive
- **Production settings.** The tenant setup applies its `production_settings` only when the site's `automotive_environment` is `production`. `new-site.sh` writes it from `ENVIRONMENT` before applying the tenant. These settings are:
  - System Settings → *Enable Two Factor Auth* = 1, method **OTP App**;
  - *Session Expiry* `08:00`;
  - login by mobile number or user name disabled.
- **Who must use OTP.** Frappe requires the OTP for every user holding at least one flagged role. That includes Administrator, who holds System Manager.
- **Staging and reference sites** do not get 2FA from the tenant setup. Enable it in System Settings if they hold sensitive data.
- **All environments:** password policy on (minimum score 3) and *Apply Strict User Permissions*.
- **Verify on production:**
  - System Settings shows 2FA enabled with method OTP App.
  - Each of the five roles has *Two Factor Authentication* ticked.
  - A login as a privileged user asks for the OTP.
- **Recovery:** a System Manager uses **Reset OTP Secret** on the User form ([access-review.md](access-review.md#account-recovery)).
- **Host-level access bypasses the web login:** `bench` commands, `bench mariadb`, Docker. Treat host access as privileged access: SSH keys, a small admin group, auditing on the host.
- **Identity providers** (SSO, LDAP): none is configured by this repository; see [Identity provider](#identity-provider-sso-and-ldap).

## Identity provider (SSO and LDAP)

Frappe v16 can authenticate users against an identity provider. None is configured here
(the group is fictional); this is how to connect one without weakening the controls above.

| | OAuth 2.0 / OpenID Connect | LDAP / Active Directory |
|---|---|---|
| Configured in | **Social Login Key**: provider *Office 365* (Microsoft Entra ID), *Google*, *Keycloak*, *Frappe*, *GitHub* or *Custom* | **LDAP Settings** |
| Login | a button on `/login`; register `https://<site>` + the key's *Redirect URL* at the provider (custom providers: `/api/method/frappe.integrations.oauth2_logins.custom/<provider name>`) | the normal login form, checked against the directory |
| Frappe's OTP for the flagged roles | **not asked**: Frappe trusts the provider's login, so the provider must require MFA from everyone holding a flagged role | asked, as for a password login |
| Account matched by | the e-mail address the provider returns | the directory's e-mail attribute |

SAML is not supported by Frappe itself.

**Settings that keep the access model:**

1. **Accounts come from provisioning only.** User Provisioning Requests set the approved
   role profile and the company and plant scope. Set *Sign Ups* = **Deny** on the Social
   Login Key and tick *Do Not Create New User* in LDAP Settings, so a person the provider
   knows but the application does not is refused. A user created by the provider would
   have no company scope and would see every company.
2. **Roles stay in the application.** Leave LDAP's group mappings empty: LDAP adds and
   removes mapped roles at every login, outside the approved provisioning (a role profile
   puts its roles back on the next save, but users with extra roles have none). LDAP adds
   its *Default Role* at every login and requires one for System Users: create a role with
   no permissions for it (for example "Directory User").
3. **Portal Settings → Default Role stays empty**, as the tenant leaves it: Frappe adds that
   role to every user at their first social login.
4. **Deprovisioning stays in the application.** Disabling the account at the provider stops
   new logins, but an open session lasts until it expires (8 hours on production).
   Deactivate through a User Provisioning Request ([access-review.md](access-review.md#deprovisioning)),
   which also ends the sessions and removes API keys; a disabled user is refused by both
   login types.
5. **Password login stays on.** System Settings → *Disable Username/Password Login* turns it
   off for everyone, Administrator included: the smoke test's administrator login then
   fails and break-glass access is only through the host (`bench`). Staff who sign in
   through the provider need no password here. Password and OTP resets
   ([access-review.md](access-review.md#account-recovery)) only concern password logins;
   with a provider, account recovery is the provider's.
6. **Credentials and transport.** The client secret and the LDAP bind password are Password
   fields, encrypted with the site's `encryption_key`; rotate them at the provider, then
   paste the new value. For LDAP use `ldaps://` or *SSL/TLS Mode* StartTLS with *Require
   Trusted Certificate* = Yes. The backend reaches the provider over the stack's outbound
   network: allow it in the host firewall (findings C-03). The proxy's CSP
   (`form-action 'self'`) does not affect either flow: the provider redirects back with GET.

**Verify** with a test account: a login through the provider lands on the desk with the
provisioned roles and scope; a privileged user is asked for MFA by the provider; an account
unknown to the application, and a deactivated one, are refused.

## Dependency audit and the allowlist

```bash
./scripts/ci/audit.sh
```

What the audit does:

- Uses **pip-audit 2.9.0** from the pinned CI tool venv (`scripts/ci/tools.sh`, `.local/tools/venv`).
- Checks every pinned package of `infra/image/constraints.txt` against the PyPI and OSV advisory data.
- Options: `--strict --no-deps --disable-pip`.
- URL/VCS pins cannot be looked up by version, so they are listed as `not audited (URL pins): …`. These are Frappe's forks of `gunicorn` and `PyPika`.
- The audited list is written to `.local/constraints-audit.txt`.
- **Fails on any advisory not in `config/security/audit-allowlist.txt`.**
- The CI job `audit` runs it on every push.

**Allowlist format.** One `ADVISORY-ID  reason` per line; `#` starts a comment. The list was recorded on 2026-10-24 and holds 52 entries:

| Package | Entries |
|---|---|
| Pillow | 37 |
| pypdf | 11 |
| filelock | 2 |
| GitPython | 1 |
| pdfkit | 1 |

They are all pinned by Frappe 16.53, and each entry states its exposure, mitigation and removal condition.

**Process for a new finding:**

1. **Fix if possible.** If a fixed release is compatible with the Frappe/ERPNext pins, update the dependency through the [pin update procedure](upgrade.md#updating-the-upstream-pins), which regenerates `constraints.txt` with `scripts/image/freeze-constraints.sh`.
2. **Otherwise record it.** Add one line with:
   - the advisory id;
   - the package and why it is pinned;
   - the exposure in this system;
   - the mitigation;
   - when to remove the entry.

   Brief §8.2 gate 5: unresolved critical or high findings must be zero unless the owner explicitly accepts them. Record that acceptance in the reason or your change record.
3. **At each upstream upgrade,** remove entries that the new pins fix. The audit then flags only what remains.

**The mitigations need ClamAV.** The Pillow and pypdf entries rely on the file policy and on ClamAV scanning before use. That holds only when *Virus Scan Mode* is **Required** ([below](#file-uploads-and-clamav)). The default is Disabled.

## SBOM and image scan

```bash
./scripts/ci/sbom.sh IMAGE          # e.g. ./scripts/ci/sbom.sh toyota-erp/erp:0.1.0-72021b313d05
```

The script uses the tool images pinned in `infra/ci-tools.env`:

- **Syft** (`SYFT_IMAGE`) writes the SPDX JSON SBOM `.local/sbom/<image with /:@ replaced by _>.spdx.json`, and prints the package count.
- **Trivy** (`TRIVY_IMAGE`) scans for vulnerabilities into `.local/sbom/<…>.trivy.json`, and prints the counts per severity.
- **The script fails on fixable CRITICAL vulnerabilities.** HIGH ones are reported only.

CI runs it on the production image and uploads `.local/sbom/` in the `image-evidence` artifact (kept 30 days). Keep the SBOM of every released image with your release records.

## File uploads and ClamAV

Policy lives in Desk → **Automotive Settings** (`apps/automotive_core/automotive_core/files/policy.py`):

| Setting | Default | Effect |
|---|---|---|
| Max Upload MB | 25 | size limit (nginx `CLIENT_MAX_BODY_SIZE` is also 25m) |
| Allowed Extensions | `pdf,png,jpg,jpeg,csv,xlsx,txt,edi,json,xml` | extension allowlist |
| Virus Scan Mode | **Disabled** | `Disabled`, `Best Effort` (scan, never block) or `Required` (unscanned or infected files cannot be read or downloaded) |
| ClamAV (clamd) Host / Port | empty / 3310 | `clamav` / `3310` with `compose.clamav.yaml` |
| Default Retention Days | 0 | the period for attachments of DocTypes without their own rule; the system's own records keep theirs (`files/policy.py`, `KEPT_DOCTYPES`); 0 keeps them |
| Per-DocType Rules (Attachment Policy Rule) | none | per DocType: allowed extensions, max size, retention days, *private only* (default on) |

Checks on every new attachment, except framework-internal ones such as Prepared Report, Data Import and Repost Item Valuation:

1. Extension allowlist.
2. Size limit.
3. Content sniffing: the magic bytes of `pdf`, `png`, `jpg`/`jpeg` and `xlsx` must match the extension.
4. Attachments to business documents must be **private**, unless a rule allows otherwise.

After upload:

- The file's SHA-256 is stored.
- *Malware Scan* is set to:
  - `Not Scanned` when scanning is Disabled;
  - `Scanner Not Configured` when no host is set;
  - otherwise a scan job runs on the `short` queue and sets `Clean`, `Infected` or `Scan Failed`.
- Non-clean results are written to the audit chain.

With **Required**, users other than Administrator cannot read, print or download a file whose status is not `Clean`.

**Enabling scanning on an existing site:**

- Files uploaded before scanning was enabled stay `Not Scanned`, so they become unreadable under Required.
- `Scan Failed` files (for example clamd was down) are not retried automatically.
- Re-scan such files one at a time. A bulk re-scan is **not implemented**.

  ```bash
  ./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" execute \
    automotive_core.files.policy.scan_file --kwargs "{'file_name': '<File record name>'}"
  ```

**ClamAV service** (`CLAMAV=1`, the default):

- Image `CLAMAV_IMAGE`, signatures in the `clamav-db` volume, `egress` network for signature updates.
- About 2 GB RAM, first start up to 5 minutes.
- **Liveness:**
  - When a clamd host is set, the metrics endpoint pings clamd (`erp_clamav_up`).
  - `ErpVirusScannerDown` fires after 10 minutes without an answer.
  - Uploads are not refused while clamd is down. New files end as `Scan Failed` and stay unreadable under Required until re-scanned.

**Retention:** daily, `automotive_core.files.policy.apply_retention` deletes attachments older than a rule's *Retention Days*, at most 1000 per rule per day. Each purge is audited (`file.retention_purge`).

## Outbound connections

- **Application allowlist.** Partner webhooks go only to `https://` URLs whose host is listed in Automotive Settings → *Outbound Host Allowlist*. Redirects are not followed, and the timeout is 10 s ([integration-operations.md](integration-operations.md)).
- **No network-level egress filter.** The application containers and ClamAV are on the `egress` network with unrestricted outbound access, so the allowlist is enforced in the application only. Add host firewall egress rules if your policy requires them; none are provided.
- **Internal network.** The database and Redis are on the internal `backend` network and cannot reach out.

## TLS and HTTP headers

The TLS modes are described in [install-fresh-host.md](install-fresh-host.md#5-tls). Both Traefik modes use `infra/deploy/traefik/dynamic.yaml`.

**TLS options:**

- TLS 1.2 minimum, strict SNI;
- ECDHE with AES-GCM or ChaCha20 only;
- HTTP redirected to HTTPS.

**`hinode-headers` middleware:**

- `Strict-Transport-Security` one year, including subdomains;
- `X-Content-Type-Options: nosniff`;
- `X-Frame-Options: SAMEORIGIN`;
- `Referrer-Policy: strict-origin-when-cross-origin`;
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`.

- `Content-Security-Policy: frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'` (`script-src` stays open: Frappe's desk needs inline scripts; findings C-01).

**Entrypoint settings:** request headers containing underscores are deleted, the read timeout is 180 s, and the access log is on.

**Client IP behind the proxy.** nginx in `frontend` trusts `X-Forwarded-For` from `UPSTREAM_REAL_IP_ADDRESS`, which defaults to `0.0.0.0/0`. This is only safe because, in the TLS modes, the frontend is reachable only through Traefik. With `TLS_MODE=none`, clients can spoof their address. The address ends up in Approval Decisions (`client_ip`).

**Session cookies.** Frappe sets `sid` HttpOnly and SameSite=Lax, and Secure when the request
is HTTPS. Behind the TLS proxy that depends on the backend trusting the forwarded scheme
(`FORWARDED_ALLOW_IPS` in `infra/deploy/compose.hinode.yaml`, findings C-09); the production
rehearsal checks the flags on a real login. On production the session expires after 8 hours.

## Least-privilege notes

**Containers:**

- Application containers run as `frappe` (uid 1000) with:
  - all capabilities dropped and `no-new-privileges`;
  - an `init` process and a private `/tmp` tmpfs;
  - a PID limit on the backend.
- Scripts and configs are mounted read-only.

**Docker socket:**

- No container mounts `/var/run/docker.sock`: Traefik routes from its file provider (`infra/deploy/traefik/routes*.yaml`) and the backup and drill schedules run in the `cron` service itself (`ops/backup/cron.py`). Keep it that way: a socket mount, even read-only, grants the whole Docker API.
- `node-exporter` (monitoring) shares the host PID namespace and mounts `/` read-only.

**Exposure:**

- Only the proxy publishes ports. With `TLS_MODE=none`, the frontend does instead.
- Grafana listens on 127.0.0.1 only; Prometheus and Alertmanager are not published.

**Database and application:**

- Each site uses its own DB user. The root password is needed only for site creation, restores and drills.
- The production image does not contain the data generator ([seed-reference-data.md](seed-reference-data.md)).
- Desk roles are least-privilege templates ([access-review.md](access-review.md)). IT Operator includes System Manager: keep that role to the operations team and review it in every access review.

## Related

- [access-review.md](access-review.md)
- [install-fresh-host.md](install-fresh-host.md)
- [upgrade.md](upgrade.md)
- [integration-operations.md](integration-operations.md)
