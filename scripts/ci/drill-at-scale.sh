#!/usr/bin/env bash
# The nightly backup and the weekly restore drill on data of the acceptance size (PRF-7, OPS-4):
# a dataset.yml export (the standard dataset) restored on a reference stack, then the backup
# (bench backup with files, into a restic repository of the stack), a change after it shipped
# in the binary logs, and the drill: the newest set restored into a scratch site, brought
# forward by the logs, verified (trial balance, audit chain, record counts, attachments,
# encrypted fields). Records the sizes, how long each step took and the recovery point.
#   drill-at-scale.sh IMAGE:TAG DATASET_DIR
# Report: .local/drill/report.md, with the drill's evidence and the logs beside it.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
# shellcheck source=scripts/lib/stack.sh
source "$(dirname "$0")/../lib/stack.sh"
export LOG_TAG=drill
image=${1:?usage: drill-at-scale.sh IMAGE:TAG DATASET_DIR}
dataset=$(cd "${2:?dataset directory}" && pwd)
out=$REPO_ROOT/.local/drill
rm -rf "$out"
mkdir -p "$out"
report=$out/report.md
# shellcheck source=scripts/lib/host-install.sh
source "$(dirname "$0")/../lib/host-install.sh"
described=$(python3 - "$dataset/status.json" <<'PY' 2>/dev/null || echo "without status.json"
import json
import sys

s = json.load(open(sys.argv[1]))
print(f"{s.get('run')} ({s.get('status')}, {s.get('start_date')} .. {s.get('simulated_date')})")
PY
)
{
	echo "# Backup and restore drill at the acceptance size"
	echo
	echo "\`$image\`, dataset $described, on $(nproc) vCPU and $(free -g | awk '/Mem:/{print $2}') GB;"
	echo "$(date -u +%FT%TZ)."
	echo
	echo "| Step | Result | Seconds | Detail |"
	echo "|---|---|---|---|"
} >"$report"
finish() {
	stack_down "$out/compose.log"
	{ echo; echo "$failures failed step(s)."; } >>"$report"
	cat "$report"
	exit $((failures > 0))
}
fatal() { fail "$1"; finish; }

# the generator's dataset needs the reference image's apps; the limits leave the database room
stack_init drill 18094 "$image" GUNICORN_WORKERS=2 BACKEND_MEM_LIMIT=3g BACKEND_CPUS=2 WORKER_MEM_LIMIT=2g \
	WORKER_CPUS=2 DB_BUFFER_POOL=3G DB_MEM_LIMIT=5g DB_CPUS=3
sched() { "${stack_compose[@]}" exec -T scheduler "$@"; }

step "The dataset on a reference stack"
stack_up >"$out/stack-up.log" 2>&1 || fatal "see stack-up.log"
stack_restore "$dataset" >"$out/restore-dataset.log" 2>&1 || fatal "see restore-dataset.log"
sizes=$(sched bash -c "cd /home/frappe/frappe-bench && du -sm sites/$stack_site/public/files sites/$stack_site/private/files \
	2>/dev/null | awk '{s += \$1} END {print s}'")
# every table's estimated rows and size (data and indexes, MB), largest first (capacity.md)
"${stack_compose[@]}" exec -T -e DB_ROOT="$(<"$stack_secrets/db_root_password")" db sh -c \
	'mariadb -uroot -p"$DB_ROOT" -N -B -e "select table_name, ifnull(table_rows, 0),
	round((data_length + index_length) / 1048576, 1) from information_schema.tables
	where table_schema not in (\"mysql\", \"sys\", \"information_schema\", \"performance_schema\")
	order by data_length + index_length desc"' 2>/dev/null >"$out/tables.tsv" || true
db_mb=$(awk -F'\t' '{s += $3} END {printf "%d", s}' "$out/tables.tsv")
largest=$(head -5 "$out/tables.tsv" | awk -F'\t' '{printf "%s%s %s MB", (NR > 1 ? ", " : ""), $1, $3}')
pass "database ${db_mb:-?} MB (data and indexes; largest: ${largest:-?}), files ${sizes:-?} MB"

step "Nightly backup (bench backup with files, restic)"
if sched bash /opt/hinode/ops/backup/backup.sh >"$out/backup.log" 2>&1; then
	snapshot=$(sched restic snapshots --tag "site:$stack_site" --latest 1 --json 2>/dev/null \
		| python3 -c 'import json, sys; print(json.load(sys.stdin)[-1]["short_id"])' 2>/dev/null || true)
	# the set's own size and what the repository stores for it (compressed, deduplicated)
	size() {
		sched restic stats --mode "$1" --json 2>/dev/null \
			| python3 -c 'import json, sys; print(round(json.load(sys.stdin)["total_size"] / 1048576))' 2>/dev/null || true
	}
	pass "snapshot ${snapshot:-?}: $(size restore-size) MB backed up, $(size raw-data) MB stored in the repository"
else
	fatal "see backup.log"
fi

step "A change after the backup, shipped in the binary logs"
todo=$(stack_bench execute frappe.client.insert --kwargs \
	"{'doc': {'doctype': 'ToDo', 'description': 'Drill at scale: written after the backup'}}" \
	| grep '^{' | tail -1 | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])' || true)
if [[ -n $todo ]] && sched /home/frappe/frappe-bench/env/bin/python /opt/hinode/ops/backup/binlog.py ship \
		>"$out/binlog.log" 2>&1; then
	pass "ToDo $todo; $(tail -1 "$out/binlog.log" | cut -c1-120)"
else
	fail "ToDo '${todo:-none}' (see binlog.log)"
fi

step "Restore drill: scratch site, point-in-time replay, verification"
if sched bash /opt/hinode/ops/backup/restore-drill.sh >"$out/drill.log" 2>&1; then
	drill_ok=1
else
	drill_ok=0
fi
sched bash -c "cat \"\$(ls -t sites/$stack_site/private/ops/drills/*.json 2>/dev/null | head -1)\"" \
	>"$out/restore-drill.json" 2>/dev/null || true
sched cat "sites/$stack_site/private/ops/restore-drill-last.json" >"$out/restore-drill-last.json" 2>/dev/null || true
summary=$(python3 - "$out/restore-drill.json" "$out/restore-drill-last.json" <<'PY' 2>/dev/null || true
import json
import sys

try:
	drill = json.load(open(sys.argv[1]))
except (OSError, ValueError):
	last = json.load(open(sys.argv[2]))
	print(f"failed after {last.get('seconds')} s")
	raise SystemExit
pitr = drill.get("pitr") or {}
checks = drill.get("checks") or {}
failed = [c.get("name") for c in checks.get("checks", []) if not c.get("ok")]
print(f"restored and verified in {drill.get('restore_seconds')} s; replayed {pitr.get('logs')} log(s) to "
	f"{pitr.get('recovered_to')}, recovery point {pitr.get('rpo_seconds')} s; "
	f"{len(checks.get('checks', []))} checks, failed: {', '.join(failed) or 'none'}")
PY
)
if ((drill_ok)); then
	pass "${summary:-see restore-drill.json}"
else
	fail "${summary:-no evidence} (see drill.log)"
fi
finish
