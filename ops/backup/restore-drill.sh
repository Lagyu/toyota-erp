#!/usr/bin/env bash
# Weekly restore drill: restore the newest snapshot into a scratch site on the same
# stack, verify it, record the evidence, drop the scratch site. Proves the backups can
# actually be restored (RPO/RTO evidence for ops/runbooks/backup-restore.md).
#
# Evidence: sites/<site>/private/backups/drills/<timestamp>.json
set -euo pipefail
site=${SITE_NAME:?}
cd /home/frappe/frappe-bench
scratch="restore-drill.invalid"
work=$(mktemp -d)
evidence_dir="sites/$site/private/backups/drills"
mkdir -p "$evidence_dir"
started=$(date -u +%s)
log() { printf '%s [drill] %s\n' "$(date -u +%FT%TZ)" "$*"; }
cleanup() {
	bench drop-site "$scratch" --db-root-password "$(<"${DB_ROOT_PASSWORD_FILE:?}")" --force --no-backup \
		>/dev/null 2>&1 || true
	rm -rf "$work"
}
trap cleanup EXIT

snapshot=$(restic snapshots --host "$site" --latest 1 --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["short_id"])')
log "restoring snapshot $snapshot"
restic restore "$snapshot" --target "$work" --quiet
db=$(find "$work" -name '*-database.sql.gz' | head -1)
# bench backup --compress writes .tgz archives (.tar without it); public must not match private
public=$(find "$work" \( -name '*-files.tar' -o -name '*-files.tgz' \) ! -name '*-private-files.*' | head -1)
private=$(find "$work" \( -name '*-private-files.tar' -o -name '*-private-files.tgz' \) | head -1)
if [[ -z $public || -z $private ]]; then
	log "snapshot $snapshot holds no file archives: a backup without files cannot be restored completely"
	exit 1
fi
# the dump's trigger DEFINERs name the production account; strip them for the scratch user
zcat "$db" | sed -E 's#/\*!50017 DEFINER=`[^`]*`@`[^`]*`\*/ ?##g' | gzip -c >"$work/restore.sql.gz"

bench new-site "$scratch" --db-root-password "$(<"${DB_ROOT_PASSWORD_FILE:?}")" \
	--admin-password "$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" \
	--mariadb-user-host-login-scope='%' >/dev/null
bench --site "$scratch" restore "$work/restore.sql.gz" --with-public-files "$public" \
	--with-private-files "$private" --db-root-password "$(<"${DB_ROOT_PASSWORD_FILE:?}")" --force >/dev/null
bench --site "$scratch" migrate >/dev/null

bench --site "$scratch" execute automotive_core.ops.backup_checks.verify_restored_site \
	--kwargs "{'output': '$work/checks.json'}" >/dev/null
finished=$(date -u +%s)
evidence="$evidence_dir/$(date -u +%Y%m%dT%H%M%SZ).json"
python3 - "$work/checks.json" "$evidence" "$snapshot" "$site" "$((finished - started))" <<'PY'
import json
import sys

checks_path, evidence_path, snapshot, site, seconds = sys.argv[1:]
with open(checks_path) as handle:
	checks = json.load(handle)
with open(evidence_path, "w") as handle:
	json.dump({"snapshot": snapshot, "source_site": site, "restore_seconds": int(seconds), "checks": checks},
		handle, indent=1)
for check in checks["checks"]:
	print(("ok  " if check["ok"] else "FAIL") + " " + check["check"])
sys.exit(0 if checks["ok"] else 1)
PY
log "restore drill passed in $((finished - started))s (evidence ${evidence#sites/})"
