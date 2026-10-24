#!/usr/bin/env bash
# One time-boxed segment of a synthetic dataset run on a reference stack (CI).
#   dataset.sh IMAGE:TAG PROFILE MINUTES OUT_DIR [IN_DIR]
# A hosted CI job may run for 6 hours and the standard profile takes longer, so
# .github/workflows/dataset.yml chains segments. Each one restores the previous segment's
# export (IN_DIR), generates until the profile is complete or MINUTES have passed, stops at
# the end of the simulated day it is on (the Seed Run's stop request; the runner resumes
# from the last committed day), and exports to OUT_DIR:
#   database.sql.gz public.tgz private.tgz encryption_key   the site, restorable anywhere
#   status.json      automotive_seed.runner.status: stage, simulated date, counts, validation
#   generator.log    the generator's output
# Exit status: 0 when the run is paused or validated, 1 when it failed or its validation did.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=dataset
image=${1:?usage: dataset.sh IMAGE:TAG PROFILE MINUTES OUT_DIR [IN_DIR]}
profile=${2:?profile} minutes=${3:?minutes} out=${4:?output directory} in=${5:-}
mkdir -p "$out"
out=$(cd "$out" && pwd)
[[ -z $in ]] || in=$(cd "$in" && pwd)

stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$REPO_ROOT/.local/dataset-stack/$stamp
logs=$REPO_ROOT/.local/dataset-logs/$stamp
secrets=$work/secrets
mkdir -p "$secrets" "$logs"
chmod 700 "$secrets"
(umask 077 && for s in db_root_password admin_password metrics_token restic_password; do random_secret >"$secrets/$s"; done)
secret_for_frappe "$secrets/db_root_password" "$secrets/restic_password"
site=dataset.localhost
env_file=$work/dataset.env
# the generator runs inside the backend container (bench execute): it gets most of the memory
cat >"$env_file" <<EOF
COMPOSE_PROJECT_NAME=hinode-dataset
ENVIRONMENT=reference
SITE_NAME=$site
SITES_RULE=Host(\`$site\`)
TLS_MODE=none
HTTP_PUBLISH_PORT=127.0.0.1:18092
CUSTOM_IMAGE=${image%:*}
CUSTOM_TAG=${image##*:}
PULL_POLICY=never
CLAMAV=0
MONITORING=0
DB_PASSWORD_SECRETS_FILE=$secrets/db_root_password
ADMIN_PASSWORD_FILE=$secrets/admin_password
METRICS_TOKEN_FILE=$secrets/metrics_token
RESTIC_PASSWORD_FILE=$secrets/restic_password
RESTIC_REPOSITORY=/home/frappe/frappe-bench/sites/.restic-dataset
GUNICORN_WORKERS=2
BACKEND_MEM_LIMIT=6g
BACKEND_CPUS=3
WORKER_MEM_LIMIT=1g
WORKER_CPUS=1
DB_BUFFER_POOL=3G
DB_MEM_LIMIT=5g
DB_CPUS=3
EOF
compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$env_file")
teardown() {
	"${compose[@]}" logs --no-color >"$logs/compose.log" 2>&1 || true
	"${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
	rm -rf "$work"
}
trap teardown EXIT
bench() { "${compose[@]}" exec -T backend bench --site "$site" "$@"; }
# (the image has no pkill) SIGTERM to every generator process in the container
KILL_GENERATOR_PY='import os, signal
for pid in filter(str.isdigit, os.listdir("/proc")):
	try:
		if int(pid) != os.getpid() and b"automotive_seed.runner.run" in open(f"/proc/{pid}/cmdline", "rb").read():
			os.kill(int(pid), signal.SIGTERM)
	except OSError:
		pass'
# the last JSON line bench execute prints (other output can precede it)
bench_json() { bench execute "$@" | grep '^{' | tail -1; }
backups=/home/frappe/frappe-bench/sites/$site/private/backups

log "starting the reference stack ($image)"
"${compose[@]}" up -d --quiet-pull 2>&1 | tail -3
for _ in $(seq 1 60); do
	"${compose[@]}" exec -T db healthcheck.sh --connect >/dev/null 2>&1 && break
	sleep 5
done
"$REPO_ROOT/scripts/deploy/new-site.sh" "$env_file" 2>&1 | tail -3

if [[ -n $in ]]; then
	log "restoring the previous segment (simulated to $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("simulated_date"))' "$in/status.json"))"
	# staged on the sites volume: the container's /tmp is a small tmpfs
	for f in database.sql.gz public.tgz private.tgz; do
		"${compose[@]}" exec -T backend bash -c "cat >'$backups/restore-$f'" <"$in/$f"
	done
	"${compose[@]}" exec -T -e DB_ROOT="$(<"$secrets/db_root_password")" backend bash -c \
		"bench --site '$site' restore '$backups/restore-database.sql.gz' --with-public-files '$backups/restore-public.tgz' \
		--with-private-files '$backups/restore-private.tgz' --db-root-password \"\$DB_ROOT\" --force | tail -3 \
		&& rm -f '$backups'/restore-*"
	# encrypted fields stay readable with the dataset's own key
	if [[ -s $in/encryption_key ]]; then
		"${compose[@]}" exec -T backend python3 -c "$SET_SITE_CONFIG_PY" "$site" encryption_key <"$in/encryption_key"
	fi
	bench migrate >/dev/null
fi
# the generator runs its background work inline; scheduled jobs would race it
bench disable-scheduler >/dev/null

run=$(bench_json automotive_seed.runner.status --kwargs "{'profile': '$profile'}" \
	| python3 -c 'import json,sys; print(json.load(sys.stdin)["run"])')
log "generating $run for up to $minutes minutes"
"${compose[@]}" exec -T backend bench --site "$site" execute automotive_seed.runner.run \
	--kwargs "{'profile': '$profile'}" >"$out/generator.log" 2>&1 &
generator=$!
deadline=$((SECONDS + minutes * 60))
while kill -0 "$generator" 2>/dev/null && ((SECONDS < deadline)); do sleep 30; done
if kill -0 "$generator" 2>/dev/null; then
	log "budget spent: stopping at the end of the current simulated day"
	bench execute automotive_seed.runner.request_stop --kwargs "{'run': '$run'}" >/dev/null
	# a simulated day takes minutes; past this the process is stopped, and the day it was on
	# (one transaction) rolls back and is generated again by the next segment
	grace=$((SECONDS + 45 * 60))
	while kill -0 "$generator" 2>/dev/null && ((SECONDS < grace)); do sleep 15; done
	if kill -0 "$generator" 2>/dev/null; then
		log "the day did not finish in the grace period: stopping the generator"
		"${compose[@]}" exec -T backend python3 -c "$KILL_GENERATOR_PY" || true
	fi
fi
wait "$generator" || log "generator exited with status $? (see generator.log)"
tail -3 "$out/generator.log" || true

bench_json automotive_seed.runner.status --kwargs "{'profile': '$profile'}" >"$out/status.json"
status=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["status"], d.get("simulated_date"))' "$out/status.json")
log "run status: $status"

log "exporting the site"
bench backup --with-files --compress >/dev/null
newest() { "${compose[@]}" exec -T backend bash -c "ls -t $backups/*$1 | head -1"; }
# trigger DEFINERs name this stack's database user: strip them so any site loads the dump
"${compose[@]}" exec -T backend bash -c "zcat '$(newest -database.sql.gz)' \
	| sed -E 's#/\\*!50017 DEFINER=\`[^\`]*\`@\`[^\`]*\`\\*/ ?##g' | gzip -c" >"$out/database.sql.gz"
"${compose[@]}" cp "backend:$(newest -private-files.tgz)" "$out/private.tgz" >/dev/null
public=$("${compose[@]}" exec -T backend bash -c "ls -t $backups/*-files.tgz | grep -v private-files | head -1")
"${compose[@]}" cp "backend:$public" "$out/public.tgz" >/dev/null
"${compose[@]}" exec -T backend python3 -c \
	"import json; print(json.load(open('sites/$site/site_config.json')).get('encryption_key', ''))" >"$out/encryption_key"
chmod 600 "$out/encryption_key"
ls -la "$out" | tail -n +2

case ${status%% *} in
	Paused | Validated) exit 0 ;;
	*) exit 1 ;;
esac
