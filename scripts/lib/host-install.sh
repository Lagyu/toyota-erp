# Steps of ops/runbooks/install-fresh-host.md on a disposable test host, shared by
# scripts/ci/rehearsal.sh and scripts/ci/reboot-guest.sh. Source after common.sh, with ENV
# (the environment file), SITE, SECRETS and TLS (their directories) and report (the
# Markdown report the step rows go to) set.
# shellcheck shell=bash
# shellcheck disable=SC2154  # report is the sourcing script's

failures=0
step() { step_name=$1; step_started=$SECONDS; log "== $step_name"; }
pass() { echo "| $step_name | ok | $((SECONDS - step_started)) | ${1:-} |" >>"$report"; }
fail() { echo "| $step_name | **FAILED** | $((SECONDS - step_started)) | ${1:-} |" >>"$report"; failures=$((failures + 1)); }

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

# Step 3: the secret files with the owners and modes of the runbook, and the option file of
# the monitoring account (the account is created after the first start: monitoring_account).
# Sets exporter_password.
provision_secrets() {
	local s
	sudo install -d -m 0755 "$(dirname "$SECRETS")"
	sudo install -d -m 0750 -g "$(id -gn)" "$SECRETS"
	for s in db_root_password admin_password metrics_token restic_password grafana_admin_password; do
		sudo sh -c "umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > $SECRETS/$s"
	done
	sudo chown 1000:"$(id -gn)" "$SECRETS/db_root_password" && sudo chmod 0440 "$SECRETS/db_root_password"
	sudo chown 1000 "$SECRETS/restic_password" && sudo chmod 0400 "$SECRETS/restic_password"
	sudo chown "$(id -un)" "$SECRETS/admin_password" && sudo chmod 0400 "$SECRETS/admin_password"
	sudo chown 65534:"$(id -gn)" "$SECRETS/metrics_token" && sudo chmod 0440 "$SECRETS/metrics_token"
	sudo chown 472 "$SECRETS/grafana_admin_password" && sudo chmod 0400 "$SECRETS/grafana_admin_password"
	exporter_password=$(random_secret)
	printf '[client]\nuser=exporter\npassword=%s\nhost=db\nport=3306\n' "$exporter_password" \
		| sudo tee "$SECRETS/mysqld_exporter.cnf" >/dev/null
	sudo chown 65534 "$SECRETS/mysqld_exporter.cnf" && sudo chmod 0400 "$SECRETS/mysqld_exporter.cnf"
}

# Step 5 with TLS_MODE=files: fullchain.pem and privkey.pem for SITE from a CA made for the
# run (its certificate, ca.pem, beside them for the clients; its key shredded), and SITE
# resolving to this host
test_certificate() {
	local work
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
	rm -rf "$work"
	grep -q " $SITE\$" /etc/hosts || echo "127.0.0.1 $SITE" | sudo tee -a /etc/hosts >/dev/null
}

# The S3 store standing in for the off-host repository (versitygw), on the stack's outbound
# network as an off-host repository would be reached, with the bucket already there as at a
# storage provider: restic then sees an empty repository at once, instead of retrying a
# missing bucket for 15 minutes before initialising it.
#   s3_store ACCESS_KEY SECRET_KEY BUCKET
s3_store() {
	docker_cmd run -d --name rehearsal-s3 --restart unless-stopped --network hinode_egress --network-alias s3 \
		"$VERSITYGW_IMAGE" --access "$1" --secret "$2" --port :7070 posix /tmp >/dev/null
	docker_cmd exec rehearsal-s3 mkdir -p "/tmp/$3"
}

# The monitoring account of mysqld_exporter.cnf, through the compose.sh of the release that
# runs the stack (the statements arrive on stdin)
#   monitoring_account COMPOSE_SH
monitoring_account() {
	"$1" "$ENV" exec -T -e ROOT="$(<"$SECRETS/db_root_password")" db sh -c 'mariadb -uroot -p"$ROOT"' <<SQL
CREATE USER IF NOT EXISTS 'exporter'@'%' IDENTIFIED BY '$exporter_password' WITH MAX_USER_CONNECTIONS 3;
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'%';
SQL
}

# Prometheus's active targets, read until every one is up or two minutes have passed (a
# target scraped while its service was starting stays down until its next scrape, 30 s
# later). Sets up and total, and why: the jobs that are down with their last errors.
#   monitoring_targets COMPOSE_SH
monitoring_targets() {
	local targets summary down _
	for _ in $(seq 1 12); do
		targets=$("$1" "$ENV" exec -T prometheus wget -qO- 'http://localhost:9090/api/v1/targets?state=active' 2>/dev/null || true)
		summary=$(python3 -c 'import json,sys; t=json.load(sys.stdin)["data"]["activeTargets"]; print(sum(x["health"]=="up" for x in t), len(t), ",".join(sorted(x["labels"]["job"] for x in t if x["health"]!="up")))' \
			<<<"$targets" 2>/dev/null || echo "0 0 unreadable")
		read -r up total down <<<"$summary"
		[[ $total -gt 0 && $up == "$total" ]] && break
		sleep 10
	done
	why=$(python3 -c 'import json,sys; print("; ".join(x["labels"]["job"] + ": " + (x.get("lastError") or "?")[:160] for x in json.load(sys.stdin)["data"]["activeTargets"] if x["health"] != "up"))' \
		<<<"$targets" 2>/dev/null || true)
	why=${why:-${down:-?}}
}
