#!/usr/bin/env bash
# The guest side of scripts/ci/reboot-check.sh: runs in its disposable Ubuntu VM from the
# repository tree at /srv/toyota-erp, one phase per call, by ops/runbooks/install-fresh-host.md:
#   reboot-guest.sh host            step 1 on the fresh host: Docker from the Ubuntu archive,
#                                   erp.slice, Docker started at boot
#   reboot-guest.sh install IMAGE   steps 3 to 10 with the production example's settings: secrets,
#                                   environment file, TLS from files, preflight, first start with
#                                   ClamAV, monitoring and restic on an S3 store beside the stack,
#                                   site, smoke test
#   reboot-guest.sh before          data to find again: a ToDo, a private file, an off-host backup
#   reboot-guest.sh mark            a ToDo committed just before the power loss
#   reboot-guest.sh after LABEL     step 11's checks after LABEL (the reboot, the power loss)
# Each phase appends its report rows to ~/reboot/rows.md and keeps its logs in ~/reboot/;
# exit status 1 when one of its steps failed.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
# shellcheck source=scripts/lib/host-install.sh
source "$(dirname "$0")/../lib/host-install.sh"
export LOG_TAG=reboot-guest
load_env_file "$REPO_ROOT/infra/ci-tools.env"
out=$HOME/reboot
mkdir -p "$out"
report=$out/rows.md
state=$out/state.env  # what the later phases look for (KEY=VALUE lines, the last one wins)
ENV=/etc/hinode/production.env
SITE=erp.hinode.test
SECRETS=/etc/hinode/secrets
TLS=/etc/hinode/tls
compose() { "$REPO_ROOT/scripts/deploy/compose.sh" "$ENV" "$@"; }
bench() { compose exec -T backend bench --site "$SITE" "$@"; }
remember() { echo "$1=$2" >>"$state"; }
todo() { # DESCRIPTION: a new ToDo; prints its name
	bench execute frappe.client.insert --kwargs "{'doc': {'doctype': 'ToDo', 'description': '$1'}}" \
		| grep '^{' | tail -1 | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])'
}
exists() { [[ $(bench execute frappe.db.exists --args "['ToDo', '$1']" | tail -1) == *"$1"* ]]; }
# a private file of random content in the sites volume ("new"), or an existing one by name:
# prints its name and the digest of the bytes on disk
FILE_PY='import hashlib, os, sys
import frappe
os.chdir("sites")  # Frappe opens its log files at ../logs from the sites directory
frappe.init(site=sys.argv[1], sites_path=".")
frappe.connect()
if sys.argv[2] == "new":
	doc = frappe.get_doc({"doctype": "File", "file_name": "reboot-check.txt", "is_private": 1,
		"content": os.urandom(32768).hex()})
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
else:
	doc = frappe.get_doc("File", sys.argv[2])
with open(doc.get_full_path(), "rb") as f:
	print(doc.name, hashlib.sha256(f.read()).hexdigest())
frappe.destroy()'
PING_CLAMD_PY='import socket
s = socket.create_connection(("clamav", 3310), 3)
s.sendall(b"zPING\0")
print(s.recv(16).decode().strip("\0\n"))'

phase_host() {
	step "Host preparation on the fresh host (step 1, install-docker-ubuntu.sh)"
	if ERP_SLICE_MEMORY_HIGH=infinity ERP_SLICE_MEMORY_MAX=infinity DOCKER_START_AT_BOOT=yes \
			"$REPO_ROOT/scripts/host/install-docker-ubuntu.sh" >"$out/host.log" 2>&1 \
			&& [[ $(systemctl is-enabled docker.service) == enabled ]]; then
		pass "$(dpkg-query -W -f '${Package} ${Version}' docker.io) from $(apt-cache policy docker.io \
			| awk '/\*\*\*/ {getline; print $2, $3; exit}'); erp.slice MemoryMax $(systemctl show erp.slice -p MemoryMax --value); docker.service enabled at boot"
	else
		fail "see host.log"
	fi
}

phase_install() {
	local image=${1:?image} s3_key s3_secret _
	step "Secrets (step 3)"
	provision_secrets
	pass "$(sudo find "$SECRETS" -type f | wc -l) files, owners per runbook (the deploy user is uid $(id -u))"

	step "Environment file and TLS certificate (steps 4-5)"
	sudo cp "$REPO_ROOT/config/env/production.env.example" "$ENV"
	sudo chown "$(id -un)" "$ENV" && sudo chmod 600 "$ENV"
	s3_key=reboot$(random_secret | tr -dc 'a-z0-9' | cut -c1-8) s3_secret=$(random_secret)
	# the production example with the tuning variables sized for the VM's 12 GB
	set_env "SITE_NAME=$SITE" "SITES_RULE=Host(\`$SITE\`)" "CUSTOM_IMAGE=${image%:*}" "CUSTOM_TAG=${image##*:}" \
		PULL_POLICY=never CLAMAV=1 VIRUS_SCAN_MODE=Required "RESTIC_REPOSITORY=s3:http://s3:7070/hinode-erp-reboot" \
		"RESTIC_AWS_ACCESS_KEY_ID=$s3_key" "RESTIC_AWS_SECRET_ACCESS_KEY=$s3_secret" \
		GUNICORN_WORKERS=2 BACKEND_MEM_LIMIT=2g BACKEND_CPUS=2 WORKER_MEM_LIMIT=1g DB_BUFFER_POOL=1G DB_MEM_LIMIT=2g \
		DB_CPUS=2 DB_MAX_CONNECTIONS=200
	test_certificate
	pass "TLS_MODE=files, certificate for $SITE from a test CA; MONITORING=1; CLAMAV=1; restic on S3"

	step "Preflight (step 6)"
	if "$REPO_ROOT/scripts/deploy/preflight.sh" "$ENV" >"$out/preflight.log" 2>&1; then
		pass "$(tail -1 "$out/preflight.log")"
	else
		fail "$(tail -1 "$out/preflight.log") (see preflight.log)"
		return
	fi

	step "First start (step 7)"
	if ! timeout 900 "$REPO_ROOT/scripts/deploy/compose.sh" "$ENV" up -d --quiet-pull >"$out/first-start.log" 2>&1; then
		fail "compose up failed or did not finish within 900 s (see first-start.log)"
		return
	fi
	for _ in $(seq 1 60); do compose exec -T db healthcheck.sh --connect >/dev/null 2>&1 && break; sleep 5; done
	s3_store "$s3_key" "$s3_secret" hinode-erp-reboot
	monitoring_account "$REPO_ROOT/scripts/deploy/compose.sh"
	pass "$(compose ps --format '{{.Service}}' | wc -l) services"

	step "Site and smoke test (steps 8-10)"
	if "$REPO_ROOT/scripts/deploy/new-site.sh" "$ENV" >"$out/new-site.log" 2>&1 \
			&& "$REPO_ROOT/scripts/deploy/smoke.sh" "$ENV" >"$out/smoke-install.log" 2>&1; then
		pass "$(grep -c ' ok ' "$out/smoke-install.log") checks ok"
	else
		fail "see new-site.log / smoke-install.log"
	fi
}

phase_before() {
	local name file
	step "Data before the reboot"
	if name=$(todo 'Reboot check: recorded before the reboot') \
			&& file=$(compose exec -T backend env/bin/python -c "$FILE_PY" "$SITE" new | tail -1) \
			&& [[ $file =~ ^[^\ ]+\ [0-9a-f]{64}$ ]]; then
		remember TODO "$name"
		remember FILE "${file% *}"
		remember FILE_SHA256 "${file#* }"
		pass "ToDo $name; private file ${file% *} of 64 KB, sha256 ${file#* }"
	else
		fail "the ToDo or the file could not be written"
	fi

	step "Off-host backup before the reboot"
	if compose exec -T scheduler bash /opt/hinode/ops/backup/backup.sh >"$out/backup-before.log" 2>&1; then
		pass "$(tail -1 "$out/backup-before.log" | cut -c1-120)"
	else
		fail "see backup-before.log"
	fi
	remember BOOT_ID "$(cat /proc/sys/kernel/random/boot_id)"
}

phase_mark() {
	local name
	step "Data committed just before the power loss"
	if name=$(todo 'Reboot check: committed just before the power loss'); then
		remember POWER_TODO "$name"
		# this file only: what the check is about is the database's own commit
		sync "$state"
		pass "ToDo $name"
	else
		fail "the ToDo could not be written"
	fi
}

phase_after() {
	local label=${1:?label} slug boot booted services pending s3 digest sched workers cron pong _
	local missing=()
	slug=$(tr 'A-Z ' 'a-z-' <<<"$label")
	load_env_file "$state"
	booted=$(date -u -d "$(uptime -s)" +%Y-%m-%dT%H:%M:%SZ)

	step "$label: Docker started at boot"
	boot=$(cat /proc/sys/kernel/random/boot_id)
	if [[ $boot != "$BOOT_ID" && $(systemctl is-active docker.service) == active \
			&& $(systemctl show docker.service -p Slice --value) == erp.slice ]]; then
		pass "a new boot at $booted; docker.service active in erp.slice"
	else
		fail "boot ${boot:0:8} (before: ${BOOT_ID:0:8}); docker.service $(systemctl is-active docker.service), slice $(systemctl show docker.service -p Slice --value)"
	fi
	remember BOOT_ID "$boot"

	step "$label: every service back under its restart policy"
	# up to ten minutes for every service to run (and be healthy, where it has a health check)
	# and for the one-shot configurator to have exited 0
	for _ in $(seq 1 60); do
		services=$(compose ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}' 2>/dev/null || true)
		pending=$(awk -F'|' '$1 == "configurator" { if ($2 != "exited" || $4 != 0) print $1 "(" $2 ")"; next }
			$2 != "running" || ($3 != "" && $3 != "healthy") { print $1 "(" $2 ($3 == "" ? "" : "/" $3) ")" }' <<<"$services")
		[[ -n $services && -z $pending ]] && break
		sleep 10
	done
	compose ps -a >"$out/ps-$slug.log" 2>&1 || true
	s3=$(docker_cmd inspect -f '{{.State.Status}}' rehearsal-s3 2>/dev/null || echo missing)
	if [[ -n $services && -z $pending && $s3 == running ]]; then
		pass "$(grep -c '|running|' <<<"$services") services running, $(grep -c '|healthy|' <<<"$services") of them healthy; configurator exited 0; the S3 store running"
	else
		fail "not back: ${pending//$'\n'/ }; the S3 store $s3 (see ps-$slug.log)"
	fi

	step "$label: the site through TLS, smoke test"
	if healthy && "$REPO_ROOT/scripts/deploy/smoke.sh" "$ENV" >"$out/smoke-$slug.log" 2>&1; then
		pass "$(grep -c ' ok ' "$out/smoke-$slug.log") checks ok"
	else
		fail "ready through TLS: $(https -s -o /dev/null -w '%{http_code}' \
			"https://$SITE/api/method/automotive_core.ops.health.ready" || true); see smoke-$slug.log"
	fi

	step "$label: data kept"
	exists "$TODO" || missing+=("ToDo $TODO")
	[[ -z ${POWER_TODO:-} ]] || exists "$POWER_TODO" || missing+=("ToDo $POWER_TODO, committed before the power loss")
	digest=$(compose exec -T backend env/bin/python -c "$FILE_PY" "$SITE" "$FILE" 2>>"$out/data-$slug.log" | tail -1 || true)
	[[ ${digest#* } == "$FILE_SHA256" ]] || missing+=("file $FILE (sha256 now '${digest#* }')")
	if ((${#missing[@]} == 0)); then
		pass "ToDo $TODO${POWER_TODO:+ and ToDo $POWER_TODO}; file $FILE with the same sha256"
	else
		fail "lost: ${missing[*]}"
	fi

	step "$label: scheduler, workers, cron schedules and ClamAV"
	sched=$(bench scheduler status 2>/dev/null | tail -1 || true)
	workers=$(compose exec -T backend bench --site "$SITE" doctor 2>/dev/null | awk -F': ' '/Workers online/ {print $2}' || true)
	cron=$(compose logs --no-color --since "$booted" cron 2>/dev/null | grep -c 'next at' || true)
	for _ in $(seq 1 60); do  # clamd loads its signatures for up to five minutes
		pong=$(compose exec -T backend env/bin/python -c "$PING_CLAMD_PY" 2>/dev/null || true)
		[[ $pong == PONG ]] && break
		sleep 10
	done
	if [[ $sched == *"is enabled"* && ${workers:-0} -ge 1 && $cron -ge 2 && $pong == PONG ]]; then
		pass "$sched; $workers workers online; the cron service logged its $cron schedules since the boot; clamd answers"
	else
		fail "scheduler '${sched:-?}', workers '${workers:-?}', cron schedules since the boot $cron, clamd '${pong:-no answer}'"
	fi

	step "$label: monitoring targets"
	monitoring_targets "$REPO_ROOT/scripts/deploy/compose.sh"
	if [[ $total -gt 0 && $up == "$total" ]]; then
		pass "$up of $total targets up"
	else
		fail "$up of $total up; down: $why"
	fi

	step "$label: off-host backup"
	if compose exec -T scheduler bash /opt/hinode/ops/backup/backup.sh >"$out/backup-$slug.log" 2>&1; then
		pass "$(tail -1 "$out/backup-$slug.log" | cut -c1-120)"
	else
		fail "see backup-$slug.log"
	fi
}

phase=${1:?usage: reboot-guest.sh host|install IMAGE|before|mark|after LABEL}
shift
case $phase in
	host | install | before | mark | after) "phase_$phase" "$@" ;;
	*) die "unknown phase $phase" ;;
esac
exit $((failures > 0))
