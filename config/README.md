# Configuration templates

Safe to commit: no file here holds a secret or a host-specific value.

| Path | What it is |
|---|---|
| [env/production.env.example](env/production.env.example), [env/staging.env.example](env/staging.env.example), [env/reference.env.example](env/reference.env.example) | environment files of `scripts/deploy/compose.sh` and the deploy scripts: copy one to `/etc/hinode/<environment>.env` and edit it ([install-fresh-host.md](../ops/runbooks/install-fresh-host.md#4-environment-file)). Passwords and tokens stay in files under `/etc/hinode/secrets`, named by the `*_FILE` variables; the S3 access key of the backup repository (`RESTIC_AWS_*`) is the one credential written into the environment file itself, so keep that file at mode 600. |
| [security/audit-allowlist.txt](security/audit-allowlist.txt) | reviewed Python advisories that `scripts/ci/audit.sh` accepts |
| [security/image-allowlist.txt](security/image-allowlist.txt) | reviewed image findings that `scripts/ci/sbom.sh` accepts |
| [i18n/glossary.ja.md](i18n/glossary.ja.md) | the Japanese terms the translations use |

Copies with real values (`config/env/*.env`) are ignored by git.
