# Security findings log

This log records each release's security findings and what was decided (requirements SEC-1,
SEC-2, SEC-6, GATE-5). Each finding has a status:

- **Fixed:** changed in the code or image.
- **Mitigated:** the exposure is reduced; a residual remains.
- **Accepted:** risk understood; waiting for an upstream release.
- **Open:** work to do.

GATE-5 requires no unresolved critical or high finding unless the owner accepts it. Every
Accepted or Open item marked "owner" needs a decision from the system owner, recorded in the
last column (name, date).

How to reproduce:
- `scripts/ci/sbom.sh IMAGE` writes the SBOM (SPDX, Syft) and the vulnerability scan (Trivy)
  to `.local/sbom/`. The scanner versions are pinned in `infra/ci-tools.env`.
- `scripts/ci/audit.sh` audits the Python dependencies.
- See also [security-operations.md](../runbooks/security-operations.md).

## Scan of 2026-10-24

| | |
|---|---|
| Image | `toyota-erp/erp:0.1.0-f37d75af8bcf` (production), CI run 40089255123 (artifact `image-evidence`) |
| Tools | Syft 1.33.0 (SBOM, SPDX JSON); Trivy 0.67.2 (vulnerabilities only) |
| Base | Debian 13.7 (frappe/base:version-16) |

Critical and high counts by component:

| Component | Critical | High | Notes |
|---|---|---|---|
| Debian packages | 9 | 183 | 41 packages; none has a fix published (V-02). |
| Python packages | 10 | 57 | 8 packages pinned by Frappe v16.53.0; Pillow 12.3.0 carries all 10 criticals (V-03). |
| Node.js packages | 0 | 11 | 6 packages: the realtime server's engine.io, socket.io-parser and ws (V-05), and the licence files kept of three build tools (V-04). |

Since the first scan of this log (image `0.1.0-862cffdfad23`: 35 critical, 353 high): the esbuild
binary, uv, npm and the build trees of the desk and ERPNext's banking screen are gone from the
runtime image, and Debian's published fixes are applied at every build.

## Vulnerabilities

| ID | Finding | Severity | Status | Action / rationale | Owner decision |
|---|---|---|---|---|---|
| V-01 | Debian packages with published fixes (libbrotli1) | High | Fixed | The runtime stage runs `apt-get upgrade` (`infra/image/Containerfile`), so each build takes Debian's security fixes; the f37d75a scan has no Debian finding with a published fix. | |
| V-02 | Debian packages without a fix (41 packages, 9 critical and 183 high: curl, libxml2, libsystemd0, gnupg, libx11, …) | Critical/High | Accepted (owner) | No fixed version exists. Rebuild and rescan weekly (`sbom.sh`), and on Debian security advisories. | |
| V-03a | `uv` 0.11.33 in the runtime image | Critical | Fixed | Removed from the runtime stage; it is only needed to build the bench. Not in the f37d75a image. | |
| V-03b | `urllib3` 2.7.0 | High | Fixed | Installed: 2.8.0 in the bench and in the system Python (`infra/image/constraints.txt`). Trivy still lists urllib3 2.7.0, msgpack 1.1.2, setuptools 70.3.0 and a second Pillow 12.3.0 without a file path: no installed copy at those versions was found; recheck with the next base image. | |
| V-03c | `Pillow` 12.3.0 (28 CVEs, fixed in 13.0) | Critical | Accepted (owner) | Frappe pins `Pillow~=12.3.0`. Mitigations: uploads are limited by type and size per DocType, ClamAV scanning is available, and attachments are private by default (`files/policy.py`). Upgrade with the Frappe patch release that moves the pin. | |
| V-03d | `pypdf` 6.19.0 | High | Accepted (owner) | Frappe pins `pypdf==6.19.0`. It only parses PDFs produced or uploaded inside the system (print, attachments). | |
| V-03e | `GitPython` 3.1.62, `filelock` 3.20.4 | High | Accepted | Pinned by Frappe (`~=3.1`, `~=3.20`). Used by bench tooling and local file locks; neither is reachable from requests. | |
| V-03f | `msgpack` 1.1.2, `setuptools` 70.3.0 | High | Accepted | Packaging and serialisation libraries; no request path reaches them. Recheck with each base image. | |
| V-03g | `pdfkit` 1.0.0 | High | Accepted (owner) | No fix released. A wrapper around wkhtmltopdf, which renders print formats from our own templates only. | |
| V-04 | Node build toolchain (esbuild/Go stdlib, postcss, svgo, vite, loader-utils, shell-quote, immutable, json5, minimatch, braces, …) | Critical/High | Fixed | The runtime image keeps only the realtime server's packages (`infra/image/prune-node-modules.js`, also over ERPNext's banking build tree) and no npm, npx, corepack or yarn. What the scan still lists of the toolchain, postcss 8.4.31, launch-editor 2.6.1 and vite 8.2.1, is the `package.json` and licence files kept of direct dependencies for licence attribution: no code. | |
| V-05 | Node runtime of the realtime server (socket.io-parser 4.2.4, engine.io 6.5.4, ws 8.11.0) | High | Accepted (owner) | Pinned by Frappe's `package.json`. The socket.io endpoint is behind the TLS proxy and needs a valid Frappe session. Upgrade with Frappe. | |

## Configuration review

| ID | Finding | Severity | Status | Action / rationale | Owner decision |
|---|---|---|---|---|---|
| C-01 | No Content-Security-Policy | Medium | Mitigated | The proxy now sends `frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'` (`infra/deploy/traefik/dynamic.yaml`). `script-src` stays open: Frappe's desk needs inline scripts and `eval`. | |
| C-02 | The proxy (Traefik) and cron (Ofelia) containers mount the Docker socket | Medium | Fixed | No container mounts the socket: Traefik routes from its file provider (`infra/deploy/traefik/routes*.yaml`, the site rule from its environment), and Ofelia is replaced by a `cron` service on the application image that runs the backup and drill schedules itself (`ops/backup/cron.py`). The production rehearsal checks that no container mounts the socket. | |
| C-03 | Outbound network traffic is not filtered at the network level | Medium | Open (owner) | The application refuses webhooks to hosts outside its allowlist (Automotive Settings). Add host firewall egress rules for the stack: the database and Redis already sit on an internal network without egress. | |
| C-04 | Partner role (Sales, Logistics, Finance, Administrator) recorded on Partner User Access but not enforced inside the portals | Medium | Fixed | Each partner user's role decides the portal functions: the portal APIs refuse the others and the pages leave them out (`automotive_supply_chain.partners.CAPABILITIES`, tests `TestPortalRoles`). | |
| C-05 | Secrets reach `bench` as command-line arguments while a site is created or restored | Low | Accepted | The database root and administrator passwords are visible in the host process list for the duration of the command. Run deployments on a dedicated host; mount `/proc` with `hidepid=invisible` on shared hosts. | |
| C-06 | No outgoing e-mail account is configured by the deploy scripts | Low | Mitigated | Notification e-mails then fail visibly (Notification Dispatch status Failed, with the reason) and the business step still completes. Configure the account at install ([install-fresh-host.md](../runbooks/install-fresh-host.md)). | |
| C-07 | Approval escalation (`escalate_after_hours`) and attachment retention (`default_retention_days`) settings have no effect | Low | Fixed | A step pending past its escalation time is escalated once by the hourly job: its approvers are reminded and the next level (the next step's approvers, the group controllers after the last step) is alerted, recorded on the step and in the audit chain. The default retention period applies to DocTypes without their own rule; the system's records keep theirs (tests `test_overdue_steps_escalate_once_to_the_next_level`, `TestAttachmentPolicy`). | |
| C-08 | Recovery point objective: nightly off-host backups only | Medium | Mitigated | The binary logs and new uploads are shipped off the host every 5 minutes and replayed onto the restored nightly set (`ops/backup/binlog.py`, `replay.sh`, `restore.sh`); every drill measures the recovery point and an alert fires when shipping stops ([backup-restore.md](../runbooks/backup-restore.md#binary-log-shipping-and-point-in-time-recovery)). Measured on test sites only: a production drill must record it before the objective is claimed. | |
| C-09 | Session cookie without the Secure flag behind the TLS proxy | Medium | Fixed | gunicorn took the scheme from `X-Forwarded-Proto` only from 127.0.0.1, and the front end is another container, so Frappe saw http: the `sid` cookie lacked Secure and generated URLs said http. The backend now trusts the forwarded scheme (`FORWARDED_ALLOW_IPS`, `infra/deploy/compose.hinode.yaml`; port 8000 is reachable only on the stack's own networks). Found while fixing the production rehearsal's cookie check, which now logs a user in and checks the flags. | |

## Next scan

Rebuild the image from the current tree (`scripts/image/build.sh --reference`) and run
`scripts/ci/sbom.sh` on it. Close V-01, V-03a and V-03b against that scan, and add the new
image's counts above.
