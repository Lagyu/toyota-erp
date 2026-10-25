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
# 3. Checks HTTPS (redirect, security headers, cookie flags), upload scanning with ClamAV
#    (an EICAR sample under Virus Scan Mode Required), simulates a reboot (the
#    Docker daemon restarts every container under its restart policy) and checks again,
#    takes an off-host backup, runs the restore drill and reads the monitoring targets.
# 4. Rolls back to the previous release by ops/runbooks/upgrade.md: its tree and image, and the
#    pre-upgrade snapshot restored as it is.
# Report: .local/rehearsal/report.md (and the logs beside it). It rewrites the Docker
# daemon configuration, /etc/hosts and /etc/hinode: never run it on a host that matters.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
# shellcheck source=scripts/lib/host-install.sh
source "$(dirname "$0")/../lib/host-install.sh"
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

{
	echo "# Production rehearsal"
	echo
	echo "Upgrade from \`$prev_image\` ($(git -C "$prev" log -1 --format='%h %s' | cut -c1-70)) to \`$image\`,"
	echo "on $(. /etc/os-release && echo "$PRETTY_NAME"), $(nproc) vCPU, $(free -g | awk '/Mem:/{print $2}') GB RAM, $(date -u +%FT%TZ)."
	echo
	echo "| Step | Result | Seconds | Detail |"
	echo "|---|---|---|---|"
} >"$report"
fatal() { fail "$1"; finish; }
finish() {
	"$REPO_ROOT/scripts/deploy/compose.sh" "$ENV" logs --no-color >"$out/compose.log" 2>&1 || true
	{ echo; echo "$failures failed step(s)."; } >>"$report"
	cat "$report"
	exit $((failures > 0))
}
compose() { "$REPO_ROOT/scripts/deploy/compose.sh" "$ENV" "$@"; }
prev_compose() { "$prev/scripts/deploy/compose.sh" "$ENV" "$@"; }
COMPOSE_UP_TIMEOUT=900
# compose up of the previous release, ended after COMPOSE_UP_TIMEOUT seconds; on failure the
# services' states and the one-shot configurator's log follow compose's output
prev_compose_up() {
	timeout "$COMPOSE_UP_TIMEOUT" "$prev/scripts/deploy/compose.sh" "$ENV" up -d "$@" && return 0
	local status=$?
	echo "== compose up ended with status $status"
	prev_compose ps -a --format '{{.Service}} {{.State}} {{.Status}}' || true
	prev_compose logs --no-color --tail 40 configurator || true
	return "$status"
}
bench() { compose exec -T backend bench --site "$SITE" "$@"; }

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
provision_secrets
pass "$(sudo find "$SECRETS" -type f | wc -l) files, owners per runbook"

step "Environment file and TLS certificate (steps 4-5)"
sudo cp "$REPO_ROOT/config/env/production.env.example" "$ENV"
sudo chown "$(id -un)" "$ENV" && sudo chmod 600 "$ENV"
s3_key=rehearsal$(random_secret | tr -dc 'a-z0-9' | cut -c1-8) s3_secret=$(random_secret)
set_env "SITE_NAME=$SITE" "SITES_RULE=Host(\`$SITE\`)" "CUSTOM_IMAGE=${prev_image%:*}" "CUSTOM_TAG=${prev_image##*:}" \
	PULL_POLICY=never CLAMAV=1 VIRUS_SCAN_MODE=Required "RESTIC_REPOSITORY=s3:http://s3:7070/hinode-erp-rehearsal" \
	"RESTIC_AWS_ACCESS_KEY_ID=$s3_key" "RESTIC_AWS_SECRET_ACCESS_KEY=$s3_secret" \
	GUNICORN_WORKERS=4 BACKEND_MEM_LIMIT=3g BACKEND_CPUS=3 WORKER_MEM_LIMIT=1536m DB_BUFFER_POOL=1536M DB_MEM_LIMIT=3g \
	DB_CPUS=3 DB_MAX_CONNECTIONS=300
test_certificate
pass "TLS_MODE=files, certificate for $SITE from a test CA; MONITORING=1; restic on S3"

step "Preflight (step 6)"
if "$REPO_ROOT/scripts/deploy/preflight.sh" "$ENV" >"$out/preflight.log" 2>&1; then
	pass "$(tail -1 "$out/preflight.log")"
else
	fatal "$(tail -1 "$out/preflight.log") (see preflight.log)"
fi

# 2. the previous release -----------------------------------------------------------------
step "First start of the previous release (step 7)"
# bounded: a release whose one-shot configurator restarts after it exits (d7662a7 up to 0062cc2)
# leaves its dependants waiting for ever
if ! prev_compose_up --quiet-pull >"$out/first-start.log" 2>&1; then
	fatal "compose up failed or did not finish within $COMPOSE_UP_TIMEOUT s (see first-start.log)"
fi
for _ in $(seq 1 60); do prev_compose exec -T db healthcheck.sh --connect >/dev/null 2>&1 && break; sleep 5; done
s3_store "$s3_key" "$s3_secret" hinode-erp-rehearsal
monitoring_account "$prev/scripts/deploy/compose.sh"
pass "$(prev_compose ps --format '{{.Service}}' | wc -l) services"

step "Site and smoke test of the previous release (steps 8-9)"
# releases before wait_ready smoke-test at once after setting the metrics token, inside Frappe's
# 60 s site config cache (403 for the token): such a release is smoke-tested again after it
if ! "$prev/scripts/deploy/new-site.sh" "$ENV" >"$out/new-site.log" 2>&1; then
	fatal "see new-site.log"
fi
if "$prev/scripts/deploy/smoke.sh" "$ENV" >"$out/smoke-previous.log" 2>&1; then
	pass "$(grep -c '^.*ok ' "$out/smoke-previous.log") checks ok"
else
	mv "$out/smoke-previous.log" "$out/smoke-previous-first.log"
	sleep 65
	if "$prev/scripts/deploy/smoke.sh" "$ENV" >"$out/smoke-previous.log" 2>&1; then
		pass "$(grep -c '^.*ok ' "$out/smoke-previous.log") checks ok (the second time, after the site config cache expired: smoke-previous-first.log)"
	else
		fatal "see new-site.log / smoke-previous-first.log / smoke-previous.log"
	fi
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
	pass "maintenance mode, backup, recreate, migrate, smoke: $(grep -c ' ok ' "$out/upgrade.log") checks ok"
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
headers=$(https -D - -o /dev/null "https://$SITE/login" | tr -d '\r' || true)
missing=()
for header in "strict-transport-security: max-age=31536000" "x-content-type-options: nosniff" \
		"x-frame-options: sameorigin" "content-security-policy: frame-ancestors 'self'" "referrer-policy:"; do
	grep -qi "^${header}" <<<"$headers" || missing+=("$header")
done
# the session cookie comes with a login, not with the guest login page; Administrator gets
# the OTP challenge on production, so a user without a two-factor role logs in (password on stdin)
cookie_user=rehearsal.cookie@hinode.test
cookie_password=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
compose exec -T backend env/bin/python -c 'import os, sys
import frappe
from frappe.utils.password import update_password
os.chdir("sites")  # Frappe opens its log files at ../logs from the sites directory
frappe.init(site=sys.argv[1], sites_path=".")
frappe.connect()
if not frappe.db.exists("User", sys.argv[2]):
	user = frappe.get_doc({"doctype": "User", "email": sys.argv[2], "first_name": "Rehearsal", "send_welcome_email": 0})
	user.append("roles", {"role": "Purchase User"})
	user.insert(ignore_permissions=True)
update_password(sys.argv[2], sys.stdin.read().strip())
frappe.db.commit()
frappe.destroy()' "$SITE" "$cookie_user" <<<"$cookie_password" >"$out/cookie-user.log" 2>&1 || true
# the body from an owner-only file, so curl sends its length: a body streamed from stdin goes
# out chunked, which the backend refused (501)
body=$(mktemp)
printf '{"usr": "%s", "pwd": "%s"}' "$cookie_user" "$cookie_password" >"$body"
login=$(https -D - -o "$out/cookie-login.json" -H 'Content-Type: application/json' --data-binary @"$body" \
	"https://$SITE/api/method/login" | tr -d '\r' || true)
rm -f "$body"
echo "login: $(head -1 <<<"$login")" >>"$out/cookie-user.log"
cookie=$(grep -i '^set-cookie: sid=' <<<"$login" | head -1 || true)
[[ -n $cookie ]] || missing+=("sid cookie from a login (see cookie-user.log)")
for flag in secure httponly samesite; do grep -qi "$flag" <<<"$cookie" || missing+=("sid cookie $flag"); done
if [[ $redirect =~ ^30[178]\ https:// && ${#missing[@]} -eq 0 ]]; then
	pass "HTTP $redirect; HSTS, CSP, nosniff, frame options, referrer policy; session cookie Secure, HttpOnly, SameSite"
else
	fail "redirect '$redirect'; missing: ${missing[*]:-none}"
fi

step "Uploads scanned by ClamAV (Virus Scan Mode Required)"
# this release's new-site.sh applies the environment's scanning settings (CLAMAV=1,
# VIRUS_SCAN_MODE) to the existing site; clamd loads its signatures for up to 5 minutes
"$REPO_ROOT/scripts/deploy/new-site.sh" "$ENV" >"$out/scan.log" 2>&1 || true
for _ in $(seq 1 60); do
	pong=$(compose exec -T backend env/bin/python -c 'import socket
s = socket.create_connection(("clamav", 3310), 3)
s.sendall(b"zPING\0")
print(s.recv(16).decode().strip("\0\n"))' 2>/dev/null || true)
	[[ $pong == PONG ]] && break
	sleep 10
done
# the cookie user's API key: uploads and downloads as a user other than Administrator
keys=$(compose exec -T backend env/bin/python -c 'import os, sys
import frappe
from frappe.core.doctype.user.user import generate_keys
os.chdir("sites")
frappe.init(site=sys.argv[1], sites_path=".")
frappe.connect()
keys = generate_keys(sys.argv[2])
frappe.db.commit()
print("%s:%s" % (keys["api_key"], keys["api_secret"]))
frappe.destroy()' "$SITE" "$cookie_user" 2>>"$out/scan.log" | tail -1)
# the key's length and a check of it at the backend and through the proxy (never the key itself)
direct=$(compose exec -T -e KEYS="$keys" backend sh -c "curl -s -w ' %{http_code}' -H 'Host: $SITE' \
	-H \"Authorization: token \$KEYS\" http://127.0.0.1:8000/api/method/frappe.auth.get_logged_user" 2>&1 || true)
proxied=$(https -s -w ' %{http_code}' -H "Authorization: token $keys" \
	"https://$SITE/api/method/frappe.auth.get_logged_user" 2>&1 || true)
enabled=$(bench execute frappe.db.get_value --args "['User', '$cookie_user', 'enabled']" 2>/dev/null | tail -1)
echo "api key: ${#keys} characters; user enabled: ${enabled:-?}; backend: ${direct:0:200};" \
	"through the proxy: ${proxied:0:200}" >>"$out/scan.log"
files=$(mktemp -d)
# the EICAR test file, the harmless sample every scanner reports (written in two parts, so that a
# scanner does not flag this script)
printf '%s%s' 'X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR' '-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*' >"$files/eicar.txt"
printf 'Rehearsal: a clean upload\n' >"$files/clean.txt"
# the uploads are attachments of a ToDo of the user's, as a desk user attaches a file to a document
todo=$(https -s -H "Authorization: token $keys" -H 'Content-Type: application/json' \
	--data '{"description": "Rehearsal: attachments scanned by ClamAV"}' "https://$SITE/api/resource/ToDo" \
	| python3 -c 'import json, sys; print(json.load(sys.stdin)["data"]["name"])' 2>>"$out/scan.log" || true)
echo "attachments on ToDo ${todo:-none}" >>"$out/scan.log"
upload() { # the response's status, and the error a refusal carries, go to scan.log; prints the file URL
	local response
	response=$(https -s -w '\n%{http_code}' -H "Authorization: token $keys" -F "file=@$1" -F is_private=1 \
		-F doctype=ToDo -F "docname=$todo" "https://$SITE/api/method/upload_file" || true)
	python3 -c 'import json, sys
body, status = sys.argv[1].rsplit("\n", 1)
try:
	data = json.loads(body)
except ValueError:
	data = {}
error = ""
if "exc" in data:  # the exception and the frames that raised it
	lines = json.loads(data["exc"])[0].strip().splitlines()
	frames = [ln.strip() for ln in lines if ln.strip().startswith("File ")][-8:]
	error = lines[-1] + " | " + " < ".join(reversed(frames))
if data.get("_server_messages"):
	error += " " + " ".join(json.loads(m).get("message", "") for m in json.loads(data["_server_messages"]))
print("upload %s: HTTP %s %s" % (sys.argv[2], status, error or body[:200]))' "$response" "${1##*/}" \
		>>"$out/scan.log" 2>&1 || true
	python3 -c 'import json, sys; print(json.loads(sys.argv[1])["message"]["file_url"])' "${response%$'\n'*}" \
		2>/dev/null || true
}
status_of() { bench execute frappe.db.get_value --args "['File', {'file_url': '$1'}, 'automotive_scan_status']" | tail -1; }
reads() { https -s -o /dev/null -w '%{http_code}' -H "Authorization: token $keys" "https://$SITE$1" || true; }
eicar_url=$(upload "$files/eicar.txt")
clean_url=$(upload "$files/clean.txt")
for _ in $(seq 1 24); do  # the scan jobs run on the short queue
	eicar_status=$(status_of "$eicar_url") clean_status=$(status_of "$clean_url")
	[[ $eicar_status == Infected && $clean_status == Clean ]] && break
	sleep 5
done
eicar_read=$(reads "$eicar_url") clean_read=$(reads "$clean_url")
audited=$(bench execute frappe.db.exists --args "['Audit Event', {'event_type': 'file.scan_infected'}]" | tail -1)
rm -rf "$files"
if [[ $pong == PONG && $eicar_status == Infected && $clean_status == Clean && $eicar_read == 403 \
		&& $clean_read == 200 && -n $audited ]]; then
	pass "clamd answers; the EICAR upload is Infected, audited and refused to its owner (403); a clean one reads (200)"
else
	fail "clamd '${pong:-no answer}'; EICAR ${eicar_status:-?} (read $eicar_read), clean ${clean_status:-?} (read $clean_read), audit '${audited:-none}' (see scan.log)"
fi

step "The site's own HTTPS name from inside the application (PDF style fetches)"
inside=$(compose exec -T backend curl -sk -o /dev/null -w '%{http_code}' \
	"https://$SITE/api/method/automotive_core.ops.health.live" 2>/dev/null || true)
if [[ $inside == 200 ]]; then
	pass "backend reaches https://$SITE through the proxy (alias on the egress network)"
else
	fail "backend got '$inside' for https://$SITE"
fi

step "Structured logs with correlation ids"
# a refused partner delivery is logged (automotive.log, JSON) under the request's correlation id,
# which the response echoes; the line is in the site's logs on the shared sites volume
cid="rehearsal-$RANDOM$RANDOM"
echoed=$(https -s -o /dev/null -D - -X POST -H "X-Correlation-ID: $cid" -H "X-Hinode-Partner: REHEARSAL-NO-SUCH" \
	-H "Content-Type: application/json" --data '{}' \
	"https://$SITE/api/method/automotive_core.integration_hub.inbound.receive" \
	| tr -d '\r' | awk 'tolower($1) == "x-correlation-id:" {print $2}')
logged=$(compose exec -T scheduler bash -c "grep -h '$cid' sites/$SITE/logs/automotive.log* 2>/dev/null" || true)
if [[ $echoed == "$cid" && $logged == *'"event": "integration.inbound.unknown_partner"'* ]]; then
	pass "the response echoes X-Correlation-ID; automotive.log holds the refusal under it (read from another container)"
else
	fail "echoed '${echoed:-none}'; log line '${logged:0:160}'"
fi

step "Site database password rotation (rotate-site-db-password.sh)"
# the old password stays in this shell's memory only: after the rotation it must be refused
SITE_DB_PY='import json, sys
import pymysql
site = sys.argv[1]
config = json.load(open(f"sites/{site}/site_config.json"))
common = json.load(open("sites/common_site_config.json"))
if sys.argv[2] == "show":
	print(config["db_password"])
	raise SystemExit
old = sys.stdin.read().strip()
try:
	pymysql.connect(host=config.get("db_host") or common["db_host"], port=int(common.get("db_port") or 3306),
		user=config.get("db_user") or config["db_name"], password=old).close()
	verdict = "the old password still works"
except pymysql.err.OperationalError as exc:
	verdict = "the old password is refused" if exc.args[0] == 1045 else f"error {exc.args[0]}"
print(verdict + ("; the site config holds a new one" if config["db_password"] != old else "; the site config is unchanged"))'
old_db_password=$(compose exec -T backend env/bin/python -c "$SITE_DB_PY" "$SITE" show 2>/dev/null | tail -1)
if "$REPO_ROOT/scripts/deploy/rotate-site-db-password.sh" "$ENV" >"$out/rotate-db.log" 2>&1; then
	verdict=$(printf '%s' "$old_db_password" | compose exec -T backend env/bin/python -c "$SITE_DB_PY" "$SITE" check 2>&1 | tail -1)
	if [[ $verdict == "the old password is refused; the site config holds a new one" ]]; then
		pass "$verdict; $(grep -c ' ok ' "$out/rotate-db.log") smoke checks ok after the restart"
	else
		fail "$verdict (see rotate-db.log)"
	fi
else
	fail "see rotate-db.log"
fi
unset old_db_password

# 5. reboot, backup, restore drill, monitoring --------------------------------------------
step "Simulated reboot (Docker daemon restart)"
sudo systemctl restart docker
if healthy && "$REPO_ROOT/scripts/deploy/smoke.sh" "$ENV" >"$out/smoke-reboot.log" 2>&1 \
		&& [[ $(bench execute frappe.db.exists --args "['ToDo', '$before']" | tail -1) == *"$before"* ]]; then
	pass "every service back under its restart policy; smoke test ok; data kept"
else
	fail "see smoke-reboot.log"
fi

step "Off-host backup (restic on S3), binary log shipping and restore drill with point-in-time recovery"
# a change after the backup that only the shipped binary logs hold
if compose exec -T scheduler bash /opt/hinode/ops/backup/backup.sh >"$out/backup.log" 2>&1 \
		&& bench execute frappe.client.insert --kwargs \
			"{'doc': {'doctype': 'ToDo', 'description': 'Rehearsal: recorded after the backup'}}" >/dev/null \
		&& compose exec -T scheduler /home/frappe/frappe-bench/env/bin/python /opt/hinode/ops/backup/binlog.py ship \
			>"$out/binlog.log" 2>&1 \
		&& compose exec -T scheduler bash /opt/hinode/ops/backup/restore-drill.sh >"$out/drill.log" 2>&1; then
	evidence=$(compose exec -T scheduler bash -c "cat \"\$(ls -t sites/$SITE/private/ops/drills/*.json | head -1)\"")
	pitr=$(python3 -c 'import json, sys; p = json.load(sys.stdin).get("pitr") or {}
print("replayed to %s, recovery point %s s" % (p["recovered_to"], p["rpo_seconds"]) if p.get("replayed") else "")' \
		<<<"$evidence" 2>/dev/null || true)
	if [[ -n $pitr ]]; then
		pass "$(tail -1 "$out/backup.log" | cut -c1-100); drill with point-in-time recovery: $pitr"
	else
		fail "the drill restored the backup but did not replay the shipped binary logs (see drill.log)"
	fi
else
	fail "see backup.log / binlog.log / drill.log"
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

step "A scheduled backup and restore drill by the cron service"
# the nightly backup and the weekly drill brought forward to the next minutes (UTC, seconds
# first), the cron service recreated with them and given its own schedules back afterwards
mapfile -t saved < <(grep -E '^(BACKUP|RESTORE_DRILL)_SCHEDULE=' "$ENV" || true)
newest() { # the newest backup set's id (retention keeps one a day per tag group: no count)
	compose exec -T scheduler restic snapshots --tag "site:$SITE" --latest 1 --json 2>/dev/null \
		| python3 -c 'import json, sys; print(json.load(sys.stdin)[-1]["short_id"])' 2>/dev/null || echo none
}
snapshot_before=$(newest)
since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
backup_at=$(date -u -d '+2 minutes' +'%M %H') drill_at=$(date -u -d '+4 minutes' +'%M %H')
set_env "BACKUP_SCHEDULE=0 $backup_at * * *" "RESTORE_DRILL_SCHEDULE=0 $drill_at * * *"
compose up -d cron >"$out/cron-schedule.log" 2>&1 || true
for _ in $(seq 1 60); do
	ran=$(compose logs --no-color --since "$since" cron 2>/dev/null | grep -cE '(backup|restore-drill): exit status 0' || true)
	[[ $ran -ge 2 ]] && break
	sleep 10
done
compose logs --no-color --since "$since" cron >>"$out/cron-schedule.log" 2>&1 || true
python3 - "$ENV" <<'PY'
import sys
path = sys.argv[1]
lines = [l for l in open(path).read().splitlines() if not l.startswith(("BACKUP_SCHEDULE=", "RESTORE_DRILL_SCHEDULE="))]
open(path, "w").write("\n".join(lines) + "\n")
PY
((${#saved[@]} == 0)) || set_env "${saved[@]}"
compose up -d cron >>"$out/cron-schedule.log" 2>&1 || true
snapshot_after=$(newest)
drill_ok=$(compose exec -T scheduler bash -c "cat sites/$SITE/private/ops/restore-drill-last.json" 2>/dev/null \
	| python3 -c 'import json, sys; print(json.load(sys.stdin).get("ok"))' 2>/dev/null || true)
if [[ $ran -ge 2 && $snapshot_after != none && $snapshot_after != "$snapshot_before" && $drill_ok == True ]]; then
	pass "backup at $backup_at and drill at $drill_at (minute hour, UTC) run by the schedule: newest snapshot $snapshot_before -> $snapshot_after, drill ok"
else
	fail "jobs ok $ran of 2, newest snapshot $snapshot_before -> $snapshot_after, drill ok '${drill_ok:-?}' (see cron-schedule.log)"
fi

step "Monitoring targets"
monitoring_targets "$REPO_ROOT/scripts/deploy/compose.sh"
if [[ $total -gt 0 && $up == "$total" ]]; then
	pass "$up of $total targets up"
else
	fail "$up of $total up; down: $why"
fi

step "Statement digests and table sizes (slow-queries.sh, mysqld-exporter)"
"$REPO_ROOT/scripts/deploy/slow-queries.sh" "$ENV" --limit 5 >"$out/slow-queries.md" 2>&1 || true
statements=$(grep -cE '^\| .+ \| [0-9]+ \| [0-9.]+ \| ' "$out/slow-queries.md" || true)
series() {
	compose exec -T prometheus wget -qO- "http://localhost:9090/api/v1/query?query=count($1)" 2>/dev/null \
		| python3 -c 'import json, sys; r = json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else 0)' \
		2>/dev/null || echo 0
}
digests=$(series mysql_perf_schema_events_statements_total)
tables=$(series erp_database_table_size_bytes)
if ((statements > 0 && digests > 0 && tables > 0)); then
	pass "$statements statements in the report; Prometheus holds $digests digest and $tables table size series"
else
	fail "$statements statements (see slow-queries.md); $digests digest and $tables table size series"
fi

# 6. rollback ------------------------------------------------------------------------------
step "Rollback to the previous release (upgrade.md)"
# the snapshot upgrade.sh took in the maintenance window, the previous release's tree and image,
# and its restore of that snapshot as it is: a release with point-in-time recovery is told
# --no-replay, since the binary logs shipped after the snapshot hold the upgrade's migration
snap=$(grep -o 'done: snapshot [0-9a-f]*' "$out/upgrade.log" | head -1 | awk '{print $3}')
no_replay=()
if grep -q -- '--no-replay' "$prev/scripts/deploy/restore.sh"; then no_replay=(--no-replay); fi
set_env "CUSTOM_TAG=${prev_image##*:}"
restored=ok
if [[ -z $snap ]] || ! prev_compose_up --remove-orphans >"$out/rollback.log" 2>&1; then
	restored=""
elif ! "$prev/scripts/deploy/restore.sh" "$ENV" "$snap" "${no_replay[@]}" <<<"$SITE" >>"$out/rollback.log" 2>&1; then
	# releases before wait_ready smoke-test at once after maintenance mode off, inside Frappe's
	# 60 s site config cache (503): the outcome is judged below, with the release's own smoke test
	restored="restore.sh reported a failure"
fi
if [[ -n $restored ]] && healthy && "$prev/scripts/deploy/smoke.sh" "$ENV" >"$out/smoke-rollback.log" 2>&1; then
	running=$(docker_cmd inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
		"$(compose ps -q backend)" 2>/dev/null || true)
	kept=$(bench execute frappe.db.exists --args "['ToDo', '$before']" | tail -1)
	later=$(bench execute frappe.db.get_value \
		--args "['ToDo', {'description': 'Rehearsal: recorded after the backup'}, 'name']" | tail -1)
	reserve=$(bench execute frappe.db.has_column --args "['Plant', 'reserve_warehouse']" | tail -1)
	if [[ $running == "$(cut -d- -f2 <<<"${prev_image##*:}")"* && $kept == *"$before"* && -z $later \
			&& ${reserve:-false} == "${had_reserve:-false}" ]]; then
		note=""; [[ $restored != ok ]] && note=" (the release's restore.sh: $restored; site healthy and smoke ok after the cache expired)"
		pass "previous revision $running on snapshot $snap: ToDo $before back, later data gone, schema as before the upgrade$note"
	else
		fail "revision '$running', ToDo '$kept', later '$later', reserve_warehouse '$reserve' (see rollback.log)"
	fi
else
	fail "snapshot '${snap:-none}'; see rollback.log / smoke-rollback.log"
fi

finish
