#!/usr/bin/env bash
# Check an environment before deploying or upgrading it. Static checks only (nothing is
# started or changed); run it as the deploying user, before compose.sh ... up.
#   preflight.sh ENV_FILE
# Checks: required settings, secret files (present, not empty, not world-readable, owned
# so the containers that read them can: uid 1000 for the frappe containers, 65534 for
# Prometheus and mysqld-exporter, 472 for Grafana; and readable by the deploying user where
# the deploy scripts read them on the host), TLS material for TLS_MODE=files (pair present, not expiring within 30 days,
# names SITE_NAME), the restic repository settings, the application image, and that the
# merged compose configuration renders. Exit 1 on any FAIL; WARN lines need a look.
set -uo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=preflight
env_file=${1:?usage: preflight.sh ENV_FILE}
load_env_file "$env_file"
fails=0 warns=0
pass() { printf 'PASS  %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; warns=$((warns + 1)); }
fail() { printf 'FAIL  %s\n' "$*"; fails=$((fails + 1)); }

for var in COMPOSE_PROJECT_NAME ENVIRONMENT SITE_NAME SITES_RULE TLS_MODE CUSTOM_IMAGE CUSTOM_TAG \
		DB_PASSWORD_SECRETS_FILE ADMIN_PASSWORD_FILE METRICS_TOKEN_FILE RESTIC_PASSWORD_FILE RESTIC_REPOSITORY; do
	if [[ -n ${!var:-} ]]; then pass "$var set"; else fail "$var is not set"; fi
done
case ${ENVIRONMENT:-} in
	production | staging | reference) pass "ENVIRONMENT=$ENVIRONMENT" ;;
	*) fail "ENVIRONMENT must be production, staging or reference" ;;
esac

# secret <file> <reader uid> <who reads it>
secret() {
	local file=$1 uid=$2 who=$3 owner mode
	if [[ ! -f $file ]]; then fail "$file missing ($who)"; return; fi
	if [[ ! -s $file ]]; then fail "$file is empty"; return; fi
	owner=$(stat -c %u "$file")
	mode=$(stat -c %a "$file")
	if (( 8#$mode & 8#007 )); then fail "$file is readable by everyone (mode $mode): chmod 0400"; return; fi
	if [[ $uid == host ]]; then
		if [[ -r $file ]]; then pass "$file readable by the deploying user"; else fail "$file: the deploying user cannot read it"; fi
		return
	fi
	if [[ $owner == "$uid" ]] && (( 8#$mode & 8#400 )); then pass "$file readable by uid $uid ($who)"
	elif [[ $(stat -c %g "$file") == "$uid" ]] && (( 8#$mode & 8#040 )); then pass "$file group-readable by gid $uid ($who)"
	else fail "$file: $who read it as uid $uid; make uid $uid the owner (mode 0400, or 0440 with a group that must also read it) — now owner $owner, mode $mode"; fi
}
secret "${DB_PASSWORD_SECRETS_FILE:-/nonexistent}" 1000 "database root password: scheduler (restore drill)"
secret "${DB_PASSWORD_SECRETS_FILE:-/nonexistent}" host "database root password: new-site.sh"
secret "${RESTIC_PASSWORD_FILE:-/nonexistent}" 1000 "restic password: scheduler (backups)"
secret "${ADMIN_PASSWORD_FILE:-/nonexistent}" host "administrator password: new-site.sh"
if [[ ${MONITORING:-0} == 1 ]]; then
	# Prometheus reads it as uid 65534 and new-site.sh on the host: chown 65534:<deploy group>, chmod 0440
	secret "${METRICS_TOKEN_FILE:-/nonexistent}" 65534 "metrics token: Prometheus"
	secret "${METRICS_TOKEN_FILE:-/nonexistent}" host "metrics token: new-site.sh"
	secret "${GRAFANA_ADMIN_PASSWORD_FILE:-/nonexistent}" 472 "Grafana admin password: Grafana"
	secret "${MYSQLD_EXPORTER_CNF:-/nonexistent}" 65534 "exporter credentials: mysqld-exporter"
else
	secret "${METRICS_TOKEN_FILE:-/nonexistent}" host "metrics token: new-site.sh"
fi

case ${TLS_MODE:-} in
	files)
		dir=${TLS_CERT_DIR:-}
		if [[ -f $dir/fullchain.pem && -f $dir/privkey.pem ]]; then
			pass "certificate pair in $dir"
			if command -v openssl >/dev/null; then
				if openssl x509 -in "$dir/fullchain.pem" -noout -checkend $((30 * 86400)) >/dev/null; then
					pass "certificate valid for at least 30 more days"
				else
					warn "certificate expires within 30 days (or already has): $(openssl x509 -in "$dir/fullchain.pem" -noout -enddate)"
				fi
				if openssl x509 -in "$dir/fullchain.pem" -noout -ext subjectAltName 2>/dev/null | grep -q "DNS:${SITE_NAME:-}"; then
					pass "certificate names $SITE_NAME"
				else
					fail "certificate does not name $SITE_NAME"
				fi
			fi
		else
			fail "TLS_MODE=files needs $dir/fullchain.pem and $dir/privkey.pem"
		fi ;;
	letsencrypt)
		if [[ -n ${LETSENCRYPT_EMAIL:-} ]]; then pass "LETSENCRYPT_EMAIL set (ports 80 and 443 must reach this host)"
		else fail "TLS_MODE=letsencrypt needs LETSENCRYPT_EMAIL"; fi ;;
	none)
		if [[ ${ENVIRONMENT:-} == production ]]; then fail "TLS_MODE=none is for local test stacks, not production"
		else warn "TLS_MODE=none: plain HTTP"; fi ;;
	*) fail "TLS_MODE must be letsencrypt, files or none" ;;
esac

case ${RESTIC_REPOSITORY:-} in
	s3:*)
		if [[ -n ${RESTIC_AWS_ACCESS_KEY_ID:-} && -n ${RESTIC_AWS_SECRET_ACCESS_KEY:-} ]]; then
			pass "restic repository on S3 with credentials"
		else
			fail "S3 restic repository without RESTIC_AWS_ACCESS_KEY_ID / RESTIC_AWS_SECRET_ACCESS_KEY"
		fi ;;
	/home/frappe/frappe-bench/sites/*)
		if [[ ${ENVIRONMENT:-} == production ]]; then fail "production backups must go off-host, not into the sites volume"
		else warn "restic repository inside the sites volume: not off-host"; fi ;;
	"") ;;
	*) warn "restic repository $RESTIC_REPOSITORY: make sure the scheduler container can reach it" ;;
esac

# the high-availability option: datastores outside the host, singletons on one host only
case ${DATASTORES:-bundled} in
	bundled) pass "MariaDB and Redis run in this stack" ;;
	external)
		missing=()
		for var in DB_HOST REDIS_CACHE REDIS_QUEUE; do [[ -n ${!var:-} ]] || missing+=("$var"); done
		if ((${#missing[@]})); then
			fail "DATASTORES=external needs ${missing[*]}"
		else
			pass "external datastores: MariaDB $DB_HOST:${DB_PORT:-3306}, Redis $REDIS_CACHE (cache) and $REDIS_QUEUE (queue)"
		fi
		if [[ ${BINLOG_SCHEDULE:-on} != off ]]; then
			warn "binary log shipping reads the external MariaDB's binary log (log_bin, ROW format, REPLICATION CLIENT): with a managed database use its point-in-time recovery and set BINLOG_SCHEDULE=off"
		fi ;;
	*) fail "DATASTORES must be bundled or external" ;;
esac
case ${SINGLETONS:-1} in
	1) pass "this host runs the scheduler and the cron service (one host of a deployment does)" ;;
	0) warn "SINGLETONS=0: no scheduler and no backups from this host; exactly one host of the deployment must run them" ;;
	*) fail "SINGLETONS must be 1 or 0" ;;
esac

image="${CUSTOM_IMAGE:-}:${CUSTOM_TAG:-}"
if docker_cmd image inspect "$image" >/dev/null 2>&1; then
	pass "image $image present"
elif [[ ${PULL_POLICY:-} == never ]]; then
	fail "image $image not present and PULL_POLICY=never"
else
	warn "image $image not present locally (will be pulled)"
fi
if "$REPO_ROOT/scripts/deploy/compose.sh" "$env_file" config --quiet 2>/tmp/preflight-compose.$$; then
	pass "compose configuration renders"
else
	fail "compose configuration: $(head -3 /tmp/preflight-compose.$$)"
fi
rm -f /tmp/preflight-compose.$$

printf '\n%d failed, %d warnings\n' "$fails" "$warns"
((fails == 0))
