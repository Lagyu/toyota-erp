#!/usr/bin/env bash
# Point-in-time recovery onto a backup set just restored into a site: the shipped binary logs
# (newest restic snapshot tagged binlog:$SITE_NAME) replayed from the position the set's dump
# recorded, up to UNTIL (UTC, "YYYY-MM-DD HH:MM:SS") or the newest shipped change, and the
# files uploaded since (newest snapshot tagged files:$SITE_NAME).
#
#   replay.sh WORK TARGET_SITE REPORT [UNTIL [REFERENCE_EPOCH]]
#
# WORK holds the restored set (*-binlog.json, *-site_config_backup.json). REPORT receives
# {"replayed", "reason" | "recovered_to", "rpo_seconds", ...}; the recovery point is measured
# from REFERENCE_EPOCH (default now): what a loss of the host at that moment would lose.
# Exit 0 when replayed, or skipped for a reason that is no fault (no position in the set, no
# logs shipped since the backup); 1 when shipped logs exist but cannot bring the set forward.
# Runs in the scheduler or cron container (ops/backup/restore-drill.sh, scripts/deploy/restore.sh).
set -euo pipefail
work=${1:?usage: replay.sh WORK TARGET_SITE REPORT [UNTIL [REFERENCE_EPOCH]]}
target=${2:?} report=${3:?} until=${4:-} reference=${5:-$(date -u +%s)}
here=$(cd "$(dirname "$0")" && pwd)
cd /home/frappe/frappe-bench
site=${SITE_NAME:?}
python=/home/frappe/frappe-bench/env/bin/python
log() { printf '%s [replay] %s\n' "$(date -u +%FT%TZ)" "$*"; }
skip() {
	python3 -c 'import json, sys; json.dump({"replayed": False, "reason": sys.argv[2]}, open(sys.argv[1], "w"))' \
		"$report" "$1"
	log "no point-in-time recovery: $1"
	exit 0
}
restic() { command restic --retry-lock 10m "$@"; }

position=$(find "$work" -name '*-binlog.json' | head -1)
[[ -n $position ]] || skip "the backup set records no binary log position"
config=$(find "$work" -name '*-site_config_backup.json' | head -1)
[[ -n $config ]] || skip "the backup set has no site config (source database name)"
db_of() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["db_name"])' "$1"; }
source_db=$(db_of "$config")
target_db=$(db_of "sites/$target/site_config.json")
read -r binlogs taken < <(restic snapshots --host "$site" --tag "binlog:$site" --latest 1 --json | python3 -c '
import json, sys
s = json.load(sys.stdin)
print(s[-1]["short_id"], s[-1]["time"][:19] + "Z") if s else print("- -")')
[[ $binlogs != - ]] || skip "no binary logs have been shipped"
recorded=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["recorded_at"])' "$position")
# a snapshot older than the backup cannot hold the logs after it: nothing shipped since yet
[[ $taken > $recorded ]] || skip "no binary logs shipped since the backup ($recorded; newest logs $taken)"

log "binary logs of snapshot $binlogs ($taken) onto $target from $(basename "$position")"
restic restore "$binlogs" --target "$work/binlog" --quiet
status=0
"$python" "$here/binlog.py" replay "$position" "$work/binlog" "$target_db" --source "$source_db" \
	${until:+--until "$until"} --report "$work/replay.json" || status=$?
if [[ $status != 0 ]]; then
	python3 -c 'import json, sys; json.dump({"replayed": False, "reason": sys.argv[2], "failed": True},
		open(sys.argv[1], "w"))' "$report" "binlog.py replay exit status $status (logs from snapshot $binlogs)"
	exit 1
fi
# the files uploaded since the set: the newest files snapshot over the target's files (shipped
# after each log flush, so it holds the file of every record just replayed)
read -r files files_taken < <(restic snapshots --host "$site" --tag "files:$site" --latest 1 --json | python3 -c '
import json, sys
s = json.load(sys.stdin)
print(s[-1]["short_id"], s[-1]["time"][:19] + "Z") if s else print("- -")')
if [[ $files != - && $files_taken > $recorded ]]; then
	restic restore "$files" --target "$work/files" --quiet
	shipped="$work/files/home/frappe/frappe-bench/sites/$site"
	for kind in public private; do
		if [[ -d $shipped/$kind/files ]]; then cp -a "$shipped/$kind/files/." "sites/$target/$kind/files/"; fi
	done
	log "files of snapshot $files ($files_taken) copied into $target"
fi
python3 - "$work/replay.json" "$report" "$reference" "$binlogs" <<'EOF'
import datetime as dt
import json
import sys

replay_path, report_path, reference, snapshot = sys.argv[1:]
result = json.load(open(replay_path))
result.update(replayed=True, binlog_snapshot=snapshot)
if result.get("recovered_to"):
	recovered = dt.datetime.fromisoformat(result["recovered_to"].replace("Z", "+00:00"))
	result["rpo_seconds"] = round(int(reference) - recovered.timestamp(), 1)
json.dump(result, open(report_path, "w"))
EOF
