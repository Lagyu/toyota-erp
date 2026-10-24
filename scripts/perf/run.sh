#!/usr/bin/env bash
# Load test on a reference stack with a restored dataset (GATE-7; PRF-2, PRF-3, TST-9).
#   run.sh IMAGE:TAG DATASET_DIR PHASE [PHASE ...]
#     PHASE  name:users:duration   e.g. steady:50:20m peak:150:10m
# DATASET_DIR is a dataset export (scripts/ci/dataset.sh: database, files, encryption key,
# status.json). The phases run one after another on the same stack, each as its own Locust
# run (perf/locustfile.py) with the scheduler and background workers running, as in
# production. Results per phase in perf/results/raw/<timestamp>/<phase>/: Locust's stats
# (JSON, CSV, HTML), the server's metrics before and after, the queue lengths sampled during
# the phase, a digest of the database's slow query log (statements over 0.5 s, literals
# masked), the database's and the files' size before and after, summary.txt and summary.json
# (perf/summary.py: p95 against the targets). Exit status: 0 when every phase met them.
#
# Runs where Docker may use most of the machine: the CI workflow .github/workflows/perf.yml
# (the stack and Locust on one hosted runner), never against production.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
# shellcheck source=scripts/lib/stack.sh
source "$(dirname "$0")/../lib/stack.sh"
export LOG_TAG=perf
image=${1:?usage: run.sh IMAGE:TAG DATASET_DIR PHASE [PHASE ...]}
dataset=${2:?dataset directory}
shift 2
(($#)) || die "at least one phase (name:users:duration) is required"
dataset=$(cd "$dataset" && pwd)
results=$REPO_ROOT/perf/results/raw/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$results"
umask 077

# Python for the load generator, pinned (not part of any image)
venv=$REPO_ROOT/.local/perf-venv
if ! "$venv/bin/locust" --version 2>/dev/null | grep -q "$(sed -n 's/^locust==//p' "$REPO_ROOT/perf/requirements.txt")"; then
	python3 -m venv "$venv"
	"$venv/bin/pip" install --quiet --disable-pip-version-check -r "$REPO_ROOT/perf/requirements.txt"
fi

stack_init perf 18093 "$image" GUNICORN_WORKERS=4 GUNICORN_THREADS=4 BACKEND_MEM_LIMIT=4g BACKEND_CPUS=3 \
	WORKER_MEM_LIMIT=1536m WORKER_CPUS=1 DB_BUFFER_POOL=4G DB_MEM_LIMIT=6g DB_CPUS=3
trap 'stack_down "$results/compose.log"' EXIT
stack_up
stack_restore "$dataset"
# background jobs are part of the reference workload
stack_bench enable-scheduler >/dev/null
"${stack_compose[@]}" restart backend frontend queue-short queue-long scheduler >/dev/null
for _ in $(seq 1 30); do
	curl -fsS -o /dev/null -H "Host: $stack_site" "${stack_url}/api/method/automotive_core.ops.health.live" 2>/dev/null && break
	sleep 5
done

# one-run passwords for the dataset's personas (0600, never printed)
stack_bench execute automotive_seed.e2e.prepare_users --kwargs "{'output': '/tmp/credentials.json'}" | tail -1
# the load shares one account per portal persona among many virtual users, where real partners
# each have their own: the per-user request limits (Automotive Settings) are lifted for the run
stack_bench execute frappe.db.set_single_value --args "['Automotive Settings', {'portal_reads_per_minute': 1000000, \
	'portal_changes_per_minute': 1000000, 'shop_floor_requests_per_minute': 1000000}]" >/dev/null
"${stack_compose[@]}" exec -T backend bash -c "cat /tmp/credentials.json && rm -f /tmp/credentials.json" \
	>"$stack_secrets/credentials.json"
as_of=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["simulated_date"])' "$dataset/status.json")
python3 - "$dataset/status.json" >"$results/dataset.txt" <<'PY'
import json, sys
s = json.load(open(sys.argv[1]))
print(f"dataset {s['run']}: {s['status']}, simulated {s.get('start_date')} .. {s.get('simulated_date')}")
for doctype, count in sorted((s.get("counts") or {}).items()):
    print(f"  {doctype}: {count}")
PY
log "$(head -1 "$results/dataset.txt"); report date $as_of"

# the database (as root in its container) and the site's files, for the per-phase records
db_query() {
	"${stack_compose[@]}" exec -T -e ROOT="$(<"$stack_secrets/db_root_password")" db \
		sh -c 'exec mariadb -uroot -p"$ROOT" -N -B -e "$1"' sh "$1"
}
db_snapshot() { # OUTPUT.json: each table's estimated rows and data and index bytes
	db_query "SELECT table_name, IFNULL(table_rows, 0), IFNULL(data_length + index_length, 0)
		FROM information_schema.tables
		WHERE table_schema NOT IN ('mysql', 'information_schema', 'performance_schema', 'sys')" \
		| python3 -c 'import json, sys
rows = [line.rstrip("\n").split("\t") for line in sys.stdin if line.strip()]
json.dump({r[0]: {"rows": int(r[1]), "bytes": int(r[2])} for r in rows}, open(sys.argv[1], "w"))' "$1" || true
}
files_size() { # OUTPUT: du -sb of the private and public files
	"${stack_compose[@]}" exec -T backend du -sb "sites/$stack_site/private/files" "sites/$stack_site/public/files" \
		>"$1" 2>/dev/null || true
}
slow_log() { # on PHASE | off
	if [[ $1 == on ]]; then
		db_query "SET GLOBAL slow_query_log = OFF; SET GLOBAL slow_query_log_file = '/var/lib/mysql/perf-slow-$2.log';
			SET GLOBAL long_query_time = 0.5; SET GLOBAL slow_query_log = ON" >/dev/null || true
	else
		db_query "SET GLOBAL slow_query_log = OFF" >/dev/null || true
	fi
}

status=0
for phase in "$@"; do
	IFS=: read -r name users duration <<<"$phase"
	[[ -n $name && -n $users && -n $duration ]] || die "phase '$phase' is not name:users:duration"
	dir=$results/$name
	mkdir -p "$dir"
	log "phase $name: $users users for $duration"
	spawn=$(((users + 9) / 10))
	# what summary.py reports with the results: the run, the code, the declared hardware
	python3 - "$dir/run.json" <<PY
import json, os, platform, sys
mem = next((int(line.split()[1]) // 1024 for line in open("/proc/meminfo") if line.startswith("MemTotal")), None)
cpu = next((line.split(":", 1)[1].strip() for line in open("/proc/cpuinfo") if line.startswith("model name")), "")
json.dump({"phase": "$name", "users": $users, "spawn_rate": $spawn, "duration": "$duration", "url": "$stack_url",
	"as_of": "$as_of", "image": "$image", "git_commit": "$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)",
	"git_dirty": $(git -C "$REPO_ROOT" status --porcelain -- apps | wc -l) > 0,
	"label": f"{os.cpu_count()} vCPU {cpu}, {mem} MiB RAM, {platform.system()} {platform.release()}; stack and load generator on one host"},
	open(sys.argv[1], "w"), indent=1)
PY
	"$venv/bin/python" "$REPO_ROOT/perf/summary.py" scrape "$stack_url" "$stack_secrets/metrics_token" \
		"$dir/metrics-before.prom" || true
	db_snapshot "$dir/db-before.json"
	files_size "$dir/files-before.txt"
	slow_log on "$name"
	"$venv/bin/python" "$REPO_ROOT/perf/summary.py" sample "$stack_url" "$stack_secrets/metrics_token" \
		"$dir/queues.txt" 15 &
	sampler=$!
	# a tenth of the users start per second, as people sign in over a few minutes (--json-file
	# names the file without its .json)
	"$venv/bin/locust" -f "$REPO_ROOT/perf/locustfile.py" --headless --host "$stack_url" \
		--users "$users" --spawn-rate "$spawn" --run-time "$duration" --stop-timeout 30 \
		--credentials "$stack_secrets/credentials.json" --as-of "$as_of" \
		--csv "$dir/locust" --html "$dir/report.html" --json-file "$dir/stats" --only-summary \
		--loglevel WARNING --logfile "$dir/locust.log" || true
	kill "$sampler" 2>/dev/null || true
	wait "$sampler" 2>/dev/null || true
	slow_log off
	# the raw slow log holds the statements' values: only the digest (literals masked) is kept
	"${stack_compose[@]}" exec -T db cat "/var/lib/mysql/perf-slow-$name.log" >"$dir/.slow.log" 2>/dev/null || true
	"$venv/bin/python" "$REPO_ROOT/perf/summary.py" slowlog "$dir/.slow.log" "$dir/slow-queries.json" || true
	rm -f "$dir/.slow.log"
	db_snapshot "$dir/db-after.json"
	files_size "$dir/files-after.txt"
	"$venv/bin/python" "$REPO_ROOT/perf/summary.py" scrape "$stack_url" "$stack_secrets/metrics_token" \
		"$dir/metrics-after.prom" || true
	"$venv/bin/python" "$REPO_ROOT/perf/summary.py" "$dir" || status=1
done
log "results in ${results#"$REPO_ROOT"/}"
exit $status
