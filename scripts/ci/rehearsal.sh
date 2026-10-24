#!/usr/bin/env bash
# Production rehearsal on a fresh, disposable Ubuntu host (GATE-6; CI: rehearsal.yml).
#   rehearsal.sh PREVIOUS_CHECKOUT PREVIOUS_IMAGE IMAGE
# 1. Installs the previous release (PREVIOUS_CHECKOUT: its source tree, PREVIOUS_IMAGE: its
#    production image) by ops/runbooks/install-fresh-host.md with the production example's
#    settings: host preparation, secrets with their owners, TLS from files (a certificate of
#    a test CA), monitoring, the restic repository on an S3 server (versitygw beside the
#    stack, standing in for the off-host store), preflight, first start, site, smoke test.
# 2. Records data, then upgrades to IMAGE with this tree's scripts/deploy/upgrade.sh
#    (ops/runbooks/upgrade.md) and checks the data, the image and the schema after it.
# 3. Checks HTTPS (redirect, security headers, cookie flags), simulates a reboot (the
#    Docker daemon restarts every container under its restart policy) and checks again,
#    takes an off-host backup, runs the restore drill and reads the monitoring targets.
# Report: .local/rehearsal/report.md (and the logs beside it). It rewrites the Docker
# daemon configuration, /etc/hosts and /etc/hinode: never run it on a host that matters.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=rehearsal
load_env_file "$REPO_ROOT/infra/ci-tools.env"
prev=$(cd "${1:?usage: rehearsal.sh PREVIOUS_CHECKOUT PREVIOUS_IMAGE IMAGE}" && pwd)
prev_image=${2:?previous image} image=${3:?image}
out=$REPO_ROOT/.local/rehearsal
mkdir -p "$out"
report=$out/report.md
ENV=/etc/hinode/production.env
SITE=erp.hinode.test
SECRETS=/etc/hinode/secrets
TLS=/etc/hinode/tls
failures=0

{
	echo "# Production rehearsal"
	echo
	echo "Upgrade from \`$prev_image\` ($(git -C "$prev" log -1 --format='%h %s' | cut -c1-70)) to \`$image\`,"
	echo "on $(. /etc/os-release && echo "$PRETTY_NAME"), $(nproc) vCPU, $(free -g | awk '/Mem:/{print $2}') GB RAM, $(date -u +%FT%TZ)."
	echo
	echo "| Step | Result | Seconds | Detail |"
	echo "|---|---|---|---|"
} >"$report"
step() { step_name=$1; step_started=$SECONDS; log "== $step_name"; }
pass() { echo "| $step_name | ok | $((SECONDS - step_started)) | ${1:-} |" >>"$report"; }
fail() { echo "| $step_name | **FAILED** | $((SECONDS - step_started)) | ${1:-} |" >>"$report"; failures=$((failures + 1)); }
fatal() { fail "$1"; finish; }
finish() {
	"$REPO_ROOT/scripts/deploy/compose.sh" "$ENV" logs --no-color >"$out/compose.log" 2>&1 || true
	{ echo; echo "$failures failed step(s)."; } >>"$report"
	cat "$report"
	exit $((failures > 0))
}
compose() { "$REPO_ROOT/scripts/deploy/compose.sh" "$ENV" "$@"; }
prev_compose() { "$prev/scripts/deploy/compose.sh" "$ENV" "$@"; }
bench() { compose exec -T backend bench --site "$SITE" "$@"; }
set_env() { # KEY=VALUE ...: replace or append lines of the environment file
	python3 - "$ENV" "$@" <<'PY'
import sys
path, pairs = sys.argv[1], [a.split("=", 1) for a in sys.argv[2:]]
lines = open(path).read().splitlines()
for key, value in pairs:
	line = f"{key}={value}"
	at = [i for i, l in enumerate(lines) if l.startswith(f"{key}=")]
	if at:
		lines[at[0]] = line
	else:
		lines.append(line)
open(path, "w").write("\n".join(lines) + "\n")
PY
}
https() { curl -sS --cacert "$TLS/ca.pem" "$@"; }
healthy() { # the site answers through the TLS proxy within the time limit
	local _
	for _ in $(seq 1 60); do
		https -fo /dev/null "https://$SITE/api/method/automotive_core.ops.health.ready" 2>/dev/null && return 0
		sleep 5
	done
	return 1
}

# 1. host -------------------------------------------------------------------------------
step "Host preparation (install-docker-ubuntu.sh)"
sudo cat /etc/docker/daemon.json 2>/dev/null | tee "$out/daemon.json.before" >/dev/null || true
if ERP_SLICE_MEMORY_HIGH=infinity ERP_SLICE_MEMORY_MAX=infinity DOCKER_START_AT_BOOT=yes \
		"$REPO_ROOT/scripts/host/install-docker-ubuntu.sh" >"$out/host.log" 2>&1 \
		&& [[ $(systemctl is-enabled docker.service) == enabled ]]; then
	pass "erp.slice MemoryMax $(systemctl show erp.slice -p MemoryMax --value); docker.service enabled at boot"
else
	fatal "see host.log"
fi

step "Secrets (runbook step 3)"
sudo install -d -m 0755 /etc/hinode
sudo install -d -m 0750 -g "$(id -gn)" "$SECRETS"
for s in db_root_password admin_password metrics_token restic_password grafana_admin_password; do
	sudo sh -c "umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > $SECRETS/$s"
done
sudo chown 1000:"$(id -gn)" "$SECRETS/db_root_password" && sudo chmod 0440 "$SECRETS/db_root_password"
sudo chown 1000 "$SECRETS/restic_password" && sudo chmod 0400 "$SECRETS/restic_password"
sudo chown "$(id -un)" "$SECRETS/admin_password" && sudo chmod 0400 "$SECRETS/admin_password"
sudo chown 65534:"$(id -gn)" "$SECRETS/metrics_token" && sudo chmod 0440 "$SECRETS/metrics_token"
sudo chown 472 "$SECRETS/grafana_admin_password" && sudo chmod 0400 "$SECRETS/grafana_admin_password"
# the monitoring account's option file (its account is created after the first start)
exporter_password=$(random_secret)
printf '[client]\nuser=exporter\npassword=%s\nhost=db\nport=3306\n' "$exporter_password" \
	| sudo tee "$SECRETS/mysqld_exporter.cnf" >/dev/null
sudo chown 65534 "$SECRETS/mysqld_exporter.cnf" && sudo chmod 0400 "$SECRETS/mysqld_exporter.cnf"
pass "$(sudo find "$SECRETS" -type f | wc -l) files, owners per runbook"

step "Environment file and TLS certificate (steps 4-5)"
sudo cp "$REPO_ROOT/infra/deploy/env/production.env.example" "$ENV"
sudo chown "$(id -un)" "$ENV" && sudo chmod 600 "$ENV"
s3_key=rehearsal$(random_secret | tr -dc 'a-z0-9' | cut -c1-8) s3_secret=$(random_secret)
set_env "SITE_NAME=$SITE" "SITES_RULE=Host(\`$SITE\`)" "CUSTOM_IMAGE=${prev_image%:*}" "CUSTOM_TAG=${prev_image##*:}" \
	PULL_POLICY=never CLAMAV=0 "RESTIC_REPOSITORY=s3:http://s3:7070/hinode-erp-rehearsal" \
	"RESTIC_AWS_ACCESS_KEY_ID=$s3_key" "RESTIC_AWS_SECRET_ACCESS_KEY=$s3_secret" \
	GUNICORN_WORKERS=4 BACKEND_MEM_LIMIT=3g BACKEND_CPUS=3 WORKER_MEM_LIMIT=1536m DB_BUFFER_POOL=2G DB_MEM_LIMIT=4g \
	DB_CPUS=3 DB_MAX_CONNECTIONS=300
sudo install -d -m 0750 -g "$(id -gn)" "$TLS"
work=$(mktemp -d)
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=Hinode Rehearsal CA" -keyout "$work/ca.key" \
	-out "$work/ca.pem" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=$SITE" -keyout "$work/privkey.pem" -out "$work/site.csr" 2>/dev/null
openssl x509 -req -in "$work/site.csr" -CA "$work/ca.pem" -CAkey "$work/ca.key" -CAcreateserial -days 90 \
	-extfile <(printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\n' "$SITE") -out "$work/fullchain.pem" 2>/dev/null
sudo install -m 0644 "$work/fullchain.pem" "$work/ca.pem" "$TLS/"
sudo install -m 0640 -g "$(id -gn)" "$work/privkey.pem" "$TLS/"
shred -u "$work/ca.key" "$work/privkey.pem" 2>/dev/null || true
grep -q " $SITE\$" /etc/hosts || echo "127.0.0.1 $SITE" | sudo tee -a /etc/hosts >/dev/null
pass "TLS_MODE=files, certificate for $SITE from a test CA; MONITORING=1; restic on S3"

step "Preflight (step 6)"
if "$REPO_ROOT/scripts/deploy/preflight.sh" "$ENV" >"$out/preflight.log" 2>&1; then
	pass "$(tail -1 "$out/preflight.log")"
else
	fatal "$(tail -1 "$out/preflight.log") (see preflight.log)"
fi

# 2. the previous release -----------------------------------------------------------------
step "First start of the previous release (step 7)"
prev_compose up -d --quiet-pull >"$out/first-start.log" 2>&1 || fatal "compose up (see first-start.log)"
for _ in $(seq 1 60); do prev_compose exec -T db healthcheck.sh --connect >/dev/null 2>&1 && break; sleep 5; done
# the S3 store on the stack's outbound network, as an off-host repository would be reached
docker_cmd run -d --name rehearsal-s3 --restart unless-stopped --network hinode_egress --network-alias s3 \
	"$VERSITYGW_IMAGE" --access "$s3_key" --secret "$s3_secret" --port :7070 posix /tmp >/dev/null
# the monitoring account of mysqld_exporter.cnf (the statements arrive on stdin)
prev_compose exec -T -e ROOT="$(<"$SECRETS/db_root_password")" db sh -c 'mariadb -uroot -p"$ROOT"' <<SQL
CREATE USER IF NOT EXISTS 'exporter'@'%' IDENTIFIED BY '$exporter_password' WITH MAX_USER_CONNECTIONS 3;
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'%';
SQL
pass "$(prev_compose ps --format '{{.Service}}' | wc -l) services"

step "Site and smoke test of the previous release (steps 8-9)"
if "$prev/scripts/deploy/new-site.sh" "$ENV" >"$out/new-site.log" 2>&1 && "$prev/scripts/deploy/smoke.sh" "$ENV" \
		>"$out/smoke-previous.log" 2>&1; then
	pass "$(grep -c '^.*ok ' "$out/smoke-previous.log") checks ok"
else
	fatal "see new-site.log / smoke-previous.log"
fi

step "Data before the upgrade"
before=$(bench execute frappe.client.insert --kwargs \
	"{'doc': {'doctype': 'ToDo', 'description': 'Rehearsal: recorded before the upgrade', 'priority': 'High'}}" \
	| grep '^{' | tail -1 | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])') || fatal "insert failed"
# (bench execute prints a result as JSON, and nothing when it is false)
had_reserve=$(bench execute frappe.db.has_column --args "['Plant', 'reserve_warehouse']" | tail -1)
pass "ToDo $before; Plant.reserve_warehouse before: ${had_reserve:-false}"

# 3. upgrade ------------------------------------------------------------------------------
step "Upgrade with scripts/deploy/upgrade.sh"
set_env "CUSTOM_TAG=${image##*:}"
if "$REPO_ROOT/scripts/deploy/upgrade.sh" "$ENV" >"$out/upgrade.log" 2>&1; then
	pass "backup, maintenance mode, recreate, migrate, smoke: $(grep -c ' ok ' "$out/upgrade.log") checks ok"
else
	fail "see upgrade.log"
fi

step "After the upgrade: image, data and schema"
running=$(docker_cmd inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
	"$(compose ps -q backend)" 2>/dev/null || true)
still=$(bench execute frappe.db.exists --args "['ToDo', '$before']" | tail -1)
has_reserve=$(bench execute frappe.db.has_column --args "['Plant', 'reserve_warehouse']" | tail -1)
if [[ $running == "$(cut -d- -f2 <<<"${image##*:}")"* && $still == *"$before"* && $has_reserve == true ]]; then
	pass "backend runs revision $running; ToDo $before kept; Plant.reserve_warehouse present"
else
	fail "revision '$running', ToDo '$still', reserve_warehouse '$has_reserve'"
fi

# 4. HTTPS ---------------------------------------------------------------------------------
step "HTTPS: redirect, headers and cookies"
redirect=$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' "http://$SITE/login" || true)
headers=$(https -D - -o /dev/null "https://$SITE/login" | tr -d '\r')
missing=()
for header in "strict-transport-security: max-age=31536000" "x-content-type-options: nosniff" \
		"x-frame-options: sameorigin" "content-security-policy: frame-ancestors 'self'" "referrer-policy:"; do
	grep -qi "^${header}" <<<"$headers" || missing+=("$header")
done
cookie=$(grep -i '^set-cookie: sid=' <<<"$headers" | head -1)
for flag in secure httponly samesite; do grep -qi "$flag" <<<"$cookie" || missing+=("sid cookie $flag"); done
if [[ $redirect =~ ^30[178]\ https:// && ${#missing[@]} -eq 0 ]]; then
	pass "HTTP $redirect; HSTS, CSP, nosniff, frame options, referrer policy; session cookie Secure, HttpOnly, SameSite"
else
	fail "redirect '$redirect'; missing: ${missing[*]:-none}"
fi

step "The site's own HTTPS name from inside the application (PDF style fetches)"
inside=$(compose exec -T backend curl -sk -o /dev/null -w '%{http_code}' \
	"https://$SITE/api/method/automotive_core.ops.health.live" 2>/dev/null || true)
if [[ $inside == 200 ]]; then
	pass "backend reaches https://$SITE through the proxy (alias on the egress network)"
else
	fail "backend got '$inside' for https://$SITE"
fi

# 5. reboot, backup, restore drill, monitoring --------------------------------------------
step "Simulated reboot (Docker daemon restart)"
sudo systemctl restart docker
if healthy && "$REPO_ROOT/scripts/deploy/smoke.sh" "$ENV" >"$out/smoke-reboot.log" 2>&1 \
		&& [[ $(bench execute frappe.db.exists --args "['ToDo', '$before']" | tail -1) == *"$before"* ]]; then
	pass "every service back under its restart policy; smoke test ok; data kept"
else
	fail "see smoke-reboot.log"
fi

step "Off-host backup (restic on S3) and restore drill"
if compose exec -T scheduler bash /opt/hinode/ops/backup/backup.sh >"$out/backup.log" 2>&1 \
		&& compose exec -T scheduler bash /opt/hinode/ops/backup/restore-drill.sh >"$out/drill.log" 2>&1; then
	pass "$(tail -1 "$out/backup.log" | cut -c1-120); drill: $(tail -1 "$out/drill.log" | cut -c1-120)"
else
	fail "see backup.log / drill.log"
fi

step "Scheduled jobs without the Docker socket"
sockets=$(for c in $(compose ps -q); do docker_cmd inspect --format '{{.Name}} {{range .Mounts}}{{.Source}} {{end}}' "$c"; done \
	| grep -c "docker.sock" || true)
next=$(compose logs --no-color cron 2>/dev/null | grep -c "next at" || true)
if [[ $(compose ps --format '{{.State}}' cron) == running && $next -ge 2 && $sockets -eq 0 ]]; then
	pass "cron service running with the backup and drill schedules; no container mounts the Docker socket"
else
	fail "cron state '$(compose ps --format '{{.State}}' cron)', schedules logged $next, socket mounts $sockets"
fi

step "Monitoring targets"
targets=$(compose exec -T prometheus wget -qO- 'http://localhost:9090/api/v1/targets?state=active' 2>/dev/null || true)
summary=$(python3 -c 'import json,sys; t=json.load(sys.stdin)["data"]["activeTargets"]; print(sum(x["health"]=="up" for x in t), len(t), ",".join(sorted(x["labels"]["job"] for x in t if x["health"]!="up")))' \
	<<<"$targets" 2>/dev/null || echo "0 0 unreadable")
read -r up total down <<<"$summary"
if [[ $total -gt 0 && $up == "$total" ]]; then pass "$up of $total targets up"; else fail "$up of $total up; down: ${down:-?}"; fi

finish
