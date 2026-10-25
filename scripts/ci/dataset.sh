#!/usr/bin/env bash
# One time-boxed segment of a synthetic dataset run on a reference stack (CI).
#   dataset.sh IMAGE:TAG PROFILE MINUTES OUT_DIR [IN_DIR]      (GRACE_MINUTES, default 45)
# A hosted CI job may run for 6 hours and the standard profile takes longer, so
# .github/workflows/dataset.yml chains segments. Each one restores the previous segment's
# export (IN_DIR), generates until the profile is complete or MINUTES have passed, stops at
# the end of the simulated day it is on (the Seed Run's stop request; the runner resumes
# from the last committed day), and exports to OUT_DIR:
#   database.sql.gz public.tgz private.tgz encryption_key   the site, restorable anywhere
#   status.json      automotive_seed.runner.status: stage, simulated date, counts, validation
#   generator.log    the generator's output
# Exit status: 0 when the run is paused or validated, or was stopped here in the middle of a
# day (status still Running: the next segment resumes it); 1 when it failed or its validation did.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
# shellcheck source=scripts/lib/stack.sh
source "$(dirname "$0")/../lib/stack.sh"
export LOG_TAG=dataset
image=${1:?usage: dataset.sh IMAGE:TAG PROFILE MINUTES OUT_DIR [IN_DIR]}
profile=${2:?profile} minutes=${3:?minutes} out=${4:?output directory} in=${5:-}
mkdir -p "$out"
out=$(cd "$out" && pwd)
[[ -z $in ]] || in=$(cd "$in" && pwd)
logs=$REPO_ROOT/.local/dataset-logs/$(date -u +%Y%m%dT%H%M%SZ)

# the generator runs inside the backend container (bench execute): it gets most of the memory
stack_init dataset 18092 "$image" GUNICORN_WORKERS=2 BACKEND_MEM_LIMIT=6g BACKEND_CPUS=3 WORKER_MEM_LIMIT=1g \
	WORKER_CPUS=1 DB_BUFFER_POOL=3G DB_MEM_LIMIT=5g DB_CPUS=3
trap 'stack_down "$logs/compose.log"' EXIT
# (the image has no pkill) SIGTERM to every generator process in the container
KILL_GENERATOR_PY='import os, signal
for pid in filter(str.isdigit, os.listdir("/proc")):
	try:
		if int(pid) != os.getpid() and b"automotive_seed.runner.run" in open(f"/proc/{pid}/cmdline", "rb").read():
			os.kill(int(pid), signal.SIGTERM)
	except OSError:
		pass'
# the last JSON line bench execute prints (other output can precede it)
bench_json() { stack_bench execute "$@" | grep '^{' | tail -1; }

stack_up
if [[ -n $in ]]; then
	log "previous segment simulated to $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("simulated_date"))' "$in/status.json")"
	stack_restore "$in"
fi
# the generator runs its background work inline; scheduled jobs would race it
stack_bench disable-scheduler >/dev/null

run=$(bench_json automotive_seed.runner.status --kwargs "{'profile': '$profile'}" \
	| python3 -c 'import json,sys; print(json.load(sys.stdin)["run"])')
log "generating $run for up to $minutes minutes"
# fixed string hashes: a resumed run is another process and must iterate as the first did
"${stack_compose[@]}" exec -T -e PYTHONHASHSEED=0 backend bench --site "$stack_site" execute automotive_seed.runner.run \
	--kwargs "{'profile': '$profile'}" >"$out/generator.log" 2>&1 &
generator=$!
deadline=$((SECONDS + minutes * 60)) stopped=0
while kill -0 "$generator" 2>/dev/null && ((SECONDS < deadline)); do sleep 30; done
if kill -0 "$generator" 2>/dev/null; then
	log "budget spent: stopping at the end of the current simulated day"
	stack_bench execute automotive_seed.runner.request_stop --kwargs "{'run': '$run'}" >/dev/null
	# a simulated day takes minutes; past this the process is stopped, and the day it was on
	# (one transaction) rolls back and is generated again by the next segment. GRACE_MINUTES=0
	# stops it at once, in the middle of a day (the interruption check of dataset.yml)
	grace=$((SECONDS + ${GRACE_MINUTES:-45} * 60))
	while kill -0 "$generator" 2>/dev/null && ((SECONDS < grace)); do sleep 15; done
	if kill -0 "$generator" 2>/dev/null; then
		log "the day did not finish in the grace period: stopping the generator"
		"${stack_compose[@]}" exec -T backend python3 -c "$KILL_GENERATOR_PY" || true
		stopped=1
	fi
fi
wait "$generator" || log "generator exited with status $? (see generator.log)"
tail -3 "$out/generator.log" || true

bench_json automotive_seed.runner.status --kwargs "{'profile': '$profile'}" >"$out/status.json"
status=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["status"], d.get("simulated_date"))' "$out/status.json")
log "run status: $status"
stack_export "$out"
ls -la "$out" | tail -n +2

case ${status%% *} in
	Paused | Validated) exit 0 ;;
	# the process stopped above could not record a pause: its day rolled back, and the
	# next segment's run resumes from the last committed day
	Running) exit $((stopped == 0)) ;;
	*) exit 1 ;;
esac
