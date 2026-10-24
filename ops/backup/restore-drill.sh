#!/usr/bin/env bash
# Weekly restore drill: restore the newest backup set into a scratch site on the same
# stack, bring it forward with the shipped binary logs (point-in-time recovery), verify it,
# record the evidence, drop the scratch site. Proves the backups can actually be restored
# and measures the recovery point and time (RPO/RTO evidence for
# ops/runbooks/backup-restore.md).
#
# Evidence: sites/<site>/private/ops/drills/<timestamp>.json (outside private/backups, which
# bench backup prunes by age)
set -euo pipefail
site=${SITE_NAME:?}
here=$(cd "$(dirname "$0")" && pwd)
cd /home/frappe/frappe-bench
scratch="restore-drill.invalid"
work=$(mktemp -d)
evidence_dir="sites/$site/private/ops/drills"
mkdir -p "$evidence_dir"
started=$(date -u +%s)
log() { printf '%s [drill] %s\n' "$(date -u +%FT%TZ)" "$*"; }
cleanup() {
	# drop-site keeps a copy of the site under archived/sites; put it in the work dir, removed below
	bench drop-site "$scratch" --db-root-password "$(<"${DB_ROOT_PASSWORD_FILE:?}")" --force --no-backup \
		--archived-sites-path "$work/archived" >/dev/null 2>&1 || true
	rm -rf "$work"
}
# outcome for the metrics endpoint (erp_restore_drill_*): written whether the drill passes or fails
snapshot=""
finish() {
	local status=$?
	python3 -c 'import json, os, sys, time
path, status, started, snapshot, pitr = sys.argv[1:]
result = {"finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": status == "0",
	"seconds": int(time.time()) - int(started), "snapshot": snapshot}
if os.path.exists(pitr):
	result["pitr"] = json.load(open(pitr))
json.dump(result, open(path, "w"))' \
		"sites/$site/private/ops/restore-drill-last.json" "$status" "$started" "$snapshot" "$work/pitr.json" || true
	cleanup
}
trap finish EXIT
restic() { command restic --retry-lock 10m "$@"; }

# the newest backup set (binary log snapshots carry the tag binlog:<site> instead)
snapshot=$(restic snapshots --host "$site" --tag "site:$site" --latest 1 --json | python3 -c 'import json,sys; print(json.load(sys.stdin)[-1]["short_id"])')
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
# encrypted fields must decrypt with the source site's key (checked below)
config=$(find "$work" -name '*-site_config_backup.json' | head -1)
[[ -z $config ]] || python3 "$(dirname "$0")/adopt_encryption_key.py" "$config" "sites/$scratch/site_config.json" >/dev/null
bench --site "$scratch" restore "$work/restore.sql.gz" --with-public-files "$public" \
	--with-private-files "$private" --db-root-password "$(<"${DB_ROOT_PASSWORD_FILE:?}")" --force >/dev/null
# point-in-time recovery: the changes since the backup from the shipped binary logs, as a
# disaster recovery applies them (before migrate, which may write)
bash "$here/replay.sh" "$work" "$scratch" "$work/pitr.json" "" "$started"
bench --site "$scratch" migrate >/dev/null

bench --site "$scratch" execute automotive_core.ops.backup_checks.verify_restored_site \
	--kwargs "{'output': '$work/checks.json'}" >/dev/null
finished=$(date -u +%s)
evidence="$evidence_dir/$(date -u +%Y%m%dT%H%M%SZ).json"
python3 - "$work/checks.json" "$evidence" "$snapshot" "$site" "$((finished - started))" "$work/pitr.json" <<'PY'
import json
import os
import sys

checks_path, evidence_path, snapshot, site, seconds, pitr_path = sys.argv[1:]
with open(checks_path) as handle:
	checks = json.load(handle)
pitr = json.load(open(pitr_path)) if os.path.exists(pitr_path) else {"replayed": False, "reason": "not attempted"}
with open(evidence_path, "w") as handle:
	json.dump({"snapshot": snapshot, "source_site": site, "restore_seconds": int(seconds), "pitr": pitr,
		"checks": checks}, handle, indent=1)
for check in checks["checks"]:
	print(("ok  " if check["ok"] else "FAIL") + " " + check["check"])
if pitr.get("replayed"):
	print(f"ok   point-in-time recovery to {pitr['recovered_to']} (recovery point {pitr['rpo_seconds']} s)")
else:
	print(f"--   no point-in-time recovery: {pitr.get('reason')}")
sys.exit(0 if checks["ok"] else 1)
PY
log "restore drill passed in $((finished - started))s (evidence ${evidence#sites/})"
