#!/usr/bin/env bash
# The application tier of the high-availability option rehearsed on one runner (PRF-6;
# ops/runbooks/high-availability.md), each host a compose project of the deploy compose:
# - the datastores: the stack's MariaDB and Redis only (project hinode-ha-ds), reached under
#   one name each over a network standing in for the hosts' LAN;
# - two application hosts from IMAGE (hinode-ha-a, hinode-ha-b) with DATASTORES=external and
#   the `sites` volume shared, standing in for shared storage; host A carries the singletons
#   (SINGLETONS=1), host B does not (SINGLETONS=0);
# - a load balancer (the deploy's Traefik) in front of both front ends, health-checking the
#   ready endpoint, as high-availability.md describes.
# Checks: both hosts serve the site; a change made through one is read through the other, a
# file written on one is read on the other; one scheduler in the deployment. Then host A fails
# (all its containers stop): the balancer keeps serving through B (the requests that failed,
# and for how long), the singletons start on B, and A returns without them.
#   ha-check.sh IMAGE:TAG
# Report: .local/ha/report.md, the hosts' logs beside it.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=ha
load_env_file "$REPO_ROOT/infra/versions.env"
image=${1:?usage: ha-check.sh IMAGE:TAG}
out=$REPO_ROOT/.local/ha
work=$REPO_ROOT/.local/ha-work  # env files, secrets, balancer config: not evidence
rm -rf "$out" "$work"
mkdir -p "$out" "$work/secrets" "$work/lb"
chmod 700 "$work/secrets"
report=$out/report.md
# shellcheck source=scripts/lib/host-install.sh
source "$(dirname "$0")/../lib/host-install.sh"
SITE=ha.hinode.test
LB=http://127.0.0.1:18120
{
	echo "# High-availability option: application tier"
	echo
	echo "\`$image\` on two application hosts sharing the datastores and the sites storage, behind a"
	echo "health-checking load balancer; $(date -u +%FT%TZ), $(nproc) vCPU, $(free -g | awk '/Mem:/{print $2}') GB."
	echo
	echo "| Step | Result | Seconds | Detail |"
	echo "|---|---|---|---|"
} >"$report"

(umask 077 && for s in db_root_password admin_password metrics_token restic_password; do
	random_secret >"$work/secrets/$s"
done)
secret_for_frappe "$work/secrets/db_root_password" "$work/secrets/restic_password"

# KEY=VALUE lines every host shares
common() {
	cat <<EOF
ENVIRONMENT=staging
SITE_NAME=$SITE
SITES_RULE=Host(\`$SITE\`)
TLS_MODE=none
CUSTOM_IMAGE=${image%:*}
CUSTOM_TAG=${image##*:}
PULL_POLICY=never
CLAMAV=0
MONITORING=0
DB_PASSWORD_SECRETS_FILE=$work/secrets/db_root_password
ADMIN_PASSWORD_FILE=$work/secrets/admin_password
METRICS_TOKEN_FILE=$work/secrets/metrics_token
RESTIC_PASSWORD_FILE=$work/secrets/restic_password
RESTIC_REPOSITORY=/home/frappe/frappe-bench/sites/.restic-ha
BACKUP_SCHEDULE=off
RESTORE_DRILL_SCHEDULE=off
BINLOG_SCHEDULE=off
GUNICORN_WORKERS=2
BACKEND_MEM_LIMIT=1536m
BACKEND_CPUS=1
WORKER_MEM_LIMIT=1g
WORKER_CPUS=1
DB_BUFFER_POOL=768M
DB_MEM_LIMIT=2g
DB_CPUS=2
EOF
}
# the datastores' host
common >"$work/ds.env"
cat >>"$work/ds.env" <<EOF
COMPOSE_PROJECT_NAME=hinode-ha-ds
HTTP_PUBLISH_PORT=127.0.0.1:18129
COMPOSE_EXTRA_FILES=$work/ds-lan.yaml
EOF
cat >"$work/ds-lan.yaml" <<'EOF'
services:
  db:
    networks: { backend: {}, ha-lan: { aliases: [ha-db] } }
  redis-cache:
    networks: { backend: {}, ha-lan: { aliases: [ha-redis-cache] } }
  redis-queue:
    networks: { backend: {}, ha-lan: { aliases: [ha-redis-queue] } }
networks:
  ha-lan: { external: true }
EOF
# an application host: HOST (a, b), PORT, SINGLETONS
app_host() {
	common >"$work/$1.env"
	cat >>"$work/$1.env" <<EOF
COMPOSE_PROJECT_NAME=hinode-ha-$1
HTTP_PUBLISH_PORT=127.0.0.1:$2
DATASTORES=external
DB_HOST=ha-db
DB_PORT=3306
REDIS_CACHE=ha-redis-cache:6379
REDIS_QUEUE=ha-redis-queue:6379
SINGLETONS=$3
COMPOSE_EXTRA_FILES=$work/$1-lan.yaml
EOF
	cat >"$work/$1-lan.yaml" <<EOF
services:
  configurator: { networks: { backend: {}, ha-lan: {} } }
  backend: { networks: { backend: {}, egress: {}, ha-lan: {} } }
  websocket: { networks: { backend: {}, ha-lan: {} } }
  queue-short: { networks: { backend: {}, egress: {}, ha-lan: {} } }
  queue-long: { networks: { backend: {}, egress: {}, ha-lan: {} } }
  scheduler: { networks: { backend: {}, egress: {}, ha-lan: {} } }
  cron: { networks: { backend: {}, egress: {}, ha-lan: {} } }
  frontend: { networks: { edge: {}, backend: {}, ha-lan: { aliases: [ha-$1-frontend] } } }
volumes:
  sites: { external: true, name: ha-sites }
networks:
  ha-lan: { external: true }
EOF
}
app_host a 18121 1
app_host b 18122 0
ds() { "$REPO_ROOT/scripts/deploy/compose.sh" "$work/ds.env" "$@"; }
host() { local h=$1; shift; "$REPO_ROOT/scripts/deploy/compose.sh" "$work/$h.env" "$@"; }
on() { local h=$1; shift; host "$h" exec -T backend bench --site "$SITE" "$@"; }
# the HTTP status of PATH through the balancer or a host's own front end (port)
status_at() { curl -s -o /dev/null -m 5 -w '%{http_code}' -H "Host: $SITE" "http://127.0.0.1:$1$2" || echo 000; }
READY=/api/method/automotive_core.ops.health.ready

finish() {
	for h in a b ds; do
		[[ -f $work/$h.env ]] && "$REPO_ROOT/scripts/deploy/compose.sh" "$work/$h.env" logs --no-color >"$out/$h.log" 2>&1 || true
	done
	docker_cmd logs ha-lb >"$out/lb.log" 2>&1 || true
	for h in a b ds; do
		[[ -f $work/$h.env ]] && "$REPO_ROOT/scripts/deploy/compose.sh" "$work/$h.env" down --volumes --remove-orphans >/dev/null 2>&1 || true
	done
	docker_cmd rm -f ha-lb >/dev/null 2>&1 || true
	docker_cmd volume rm ha-sites >/dev/null 2>&1 || true
	docker_cmd network rm ha-lan >/dev/null 2>&1 || true
	rm -rf "$work/secrets"
	{ echo; echo "$failures failed step(s)."; } >>"$report"
	cat "$report"
	exit $((failures > 0))
}
fatal() { fail "$1"; finish; }

step "Datastores host: MariaDB and Redis, reached over the hosts' network"
docker_cmd network create ha-lan >/dev/null
docker_cmd volume create ha-sites >/dev/null
if ! ds up -d --quiet-pull db redis-cache redis-queue >"$out/ds-up.log" 2>&1; then
	fatal "see ds-up.log"
fi
for _ in $(seq 1 60); do ds exec -T db healthcheck.sh --connect >/dev/null 2>&1 && break; sleep 5; done
pass "$(ds ps --format '{{.Service}}' | tr '\n' ' ')on ha-lan as ha-db, ha-redis-cache, ha-redis-queue"

step "Host A (singletons): first start and the site on the external database and shared storage"
if ! timeout 900 "$REPO_ROOT/scripts/deploy/compose.sh" "$work/a.env" up -d --quiet-pull >"$out/a-up.log" 2>&1 \
		|| ! "$REPO_ROOT/scripts/deploy/new-site.sh" "$work/a.env" >"$out/a-new-site.log" 2>&1; then
	fatal "see a-up.log / a-new-site.log"
fi
db_host=$(host a exec -T backend python3 -c 'import json; c = json.load(open("sites/common_site_config.json")); print(c["db_host"], c["redis_queue"])')
pass "site $SITE created; common site config: database and queue at $db_host"

step "Host B (no singletons): first start on the same site"
if ! timeout 900 "$REPO_ROOT/scripts/deploy/compose.sh" "$work/b.env" up -d --quiet-pull >"$out/b-up.log" 2>&1; then
	fatal "see b-up.log"
fi
wait_ready "$work/b.env" "$SITE" 0 || true
services_b=$(host b ps --format '{{.Service}}' | sort | tr '\n' ' ')
if [[ $(status_at 18122 "$READY") == 200 && $services_b != *scheduler* && $services_b != *cron* ]]; then
	pass "ready through its own front end; running: $services_b(no scheduler, no cron)"
else
	fatal "ready $(status_at 18122 "$READY"); running: $services_b"
fi

step "Load balancer in front of both hosts"
cat >"$work/lb/routes.yaml" <<EOF
http:
  routers:
    erp:
      rule: "PathPrefix(\`/\`)"
      entryPoints: [web]
      service: erp
  services:
    erp:
      loadBalancer:
        passHostHeader: true
        healthCheck:
          path: $READY
          hostname: $SITE
          interval: 2s
          timeout: 2s
        servers:
          - url: http://ha-a-frontend:8080
          - url: http://ha-b-frontend:8080
EOF
docker_cmd run -d --name ha-lb --network ha-lan -p 127.0.0.1:18120:80 -v "$work/lb:/etc/traefik/dynamic:ro" \
	"$TRAEFIK_IMAGE" --entrypoints.web.address=:80 --providers.file.directory=/etc/traefik/dynamic \
	--providers.file.watch=true --log.level=INFO >/dev/null
for _ in $(seq 1 30); do [[ $(status_at 18120 "$READY") == 200 ]] && break; sleep 2; done
codes=$(for _ in $(seq 1 20); do status_at 18120 "$READY"; echo; done | sort | uniq -c | xargs)
if [[ $codes == "20 200" ]]; then
	pass "20 of 20 requests answered 200 ($LB)"
else
	fail "answers: $codes"
fi

step "One database, one shared storage: what one host writes, the other reads"
todo=$(on a execute frappe.client.insert --kwargs \
	"{'doc': {'doctype': 'ToDo', 'description': 'HA check: written through host A'}}" \
	| grep '^{' | tail -1 | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])' || true)
seen=$(on b execute frappe.db.exists --args "['ToDo', '${todo:-none}']" | tail -1 || true)
content="ha-check $(random_secret)"
host b exec -T backend bash -c "mkdir -p sites/$SITE/private/files && printf '%s' '$content' >sites/$SITE/private/files/ha-check.txt"
read_back=$(host a exec -T backend cat "sites/$SITE/private/files/ha-check.txt" 2>/dev/null || true)
if [[ -n $todo && $seen == *"$todo"* && $read_back == "$content" ]]; then
	pass "ToDo $todo written on A is read on B; a file written on B is read on A"
else
	fail "ToDo '${todo:-none}' seen on B: '${seen:-no}'; file read on A: '${read_back:0:40}'"
fi

step "One scheduler in the deployment"
singletons() { # the scheduler and cron containers running in the deployment, by host
	local h
	for h in a b; do
		host "$h" ps --format '{{.Service}}' 2>/dev/null | grep -E '^(scheduler|cron)$' | sed "s/^/$h:/" || true
	done | sort | xargs
}
running=$(singletons)
if [[ $running == "a:cron a:scheduler" ]]; then
	pass "scheduler and cron on host A only"
else
	fail "running: ${running:-none}"
fi

step "Host A fails: the balancer serves through host B"
# a request every 200 ms through the balancer while A goes down, until 20 s after it is down
probe=$out/failover-probe.txt
(
	end=$((SECONDS + 60))
	while ((SECONDS < end)); do
		echo "$(date +%s.%N) $(status_at 18120 "$READY")"
		sleep 0.2
	done
) >"$probe" &
prober=$!
sleep 5
down_at=$(date +%s.%N)
host a stop >"$out/a-stop.log" 2>&1
wait "$prober" || true
summary=$(python3 - "$probe" "$down_at" <<'PY'
import sys
rows = [line.split() for line in open(sys.argv[1]) if len(line.split()) == 2]
down = float(sys.argv[2])
after = [(float(t), code) for t, code in rows if float(t) >= down]
failed = [(t, code) for t, code in after if code != "200"]
last_failure = max((t for t, _ in failed), default=down)
tail_ok = all(code == "200" for t, code in after if t > last_failure)
print(len(rows), len(after), len(failed), round(last_failure - down, 1), "yes" if after and tail_ok else "no")
PY
)
read -r total after failed window settled <<<"$summary"
if [[ $settled == yes && $(status_at 18120 "$READY") == 200 && $(status_at 18121 "$READY") == 000 ]]; then
	pass "after host A stopped: $failed of $after requests failed, the last ${window} s after; every later one answered 200 through host B"
else
	fail "after host A stopped: $failed of $after requests failed (window ${window} s, settled: $settled; see failover-probe.txt)"
fi

step "The singletons move to host B"
set_env_in() { # HOST KEY=VALUE: the host's environment file
	local file=$work/$1.env key=${2%%=*}
	grep -v "^$key=" "$file" >"$file.new"
	echo "$2" >>"$file.new"
	mv "$file.new" "$file"
}
set_env_in b SINGLETONS=1
host b up -d scheduler cron >"$out/b-singletons.log" 2>&1 || true
for _ in $(seq 1 30); do
	running=$(singletons)
	cron_logged=$(host b logs --no-color cron 2>/dev/null | grep -c 'next at' || true)
	[[ $running == "b:cron b:scheduler" && $cron_logged -ge 1 ]] && break
	sleep 5
done
sched=$(on b scheduler status 2>/dev/null | tail -1 || true)
if [[ $running == "b:cron b:scheduler" && $cron_logged -ge 1 && $sched == *"is enabled"* ]]; then
	pass "scheduler and cron running on host B only; the cron service logged its schedules; $sched"
else
	fail "running: ${running:-none}; cron schedules logged $cron_logged; scheduler '${sched:-?}'"
fi

step "Host A returns without the singletons"
set_env_in a SINGLETONS=0
host a up -d >"$out/a-return.log" 2>&1 || true
host a up -d --remove-orphans >>"$out/a-return.log" 2>&1 || true
wait_ready "$work/a.env" "$SITE" 0 || true
for _ in $(seq 1 30); do [[ $(status_at 18121 "$READY") == 200 ]] && break; sleep 2; done
sleep 6  # two health checks of the balancer
codes=$(for _ in $(seq 1 20); do status_at 18120 "$READY"; echo; done | sort | uniq -c | xargs)
running=$(singletons)
if [[ $(status_at 18121 "$READY") == 200 && $codes == "20 200" && $running == "b:cron b:scheduler" ]]; then
	pass "host A ready again behind the balancer (20 of 20 answered 200); singletons still on B only"
else
	fail "host A ready $(status_at 18121 "$READY"); balancer answers $codes; running: ${running:-none}"
fi

finish
