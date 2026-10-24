#!/usr/bin/env bash
# Production-shaped smoke test of an application image on this host:
#   compose (frappe_docker + Hinode overrides, TLS off, no ClamAV/monitoring, small limits)
#   -> new site -> health/readiness/portal/login checks -> one real backup into a local
#   restic repository -> restore drill into a scratch site -> tear down.
#   smoke.sh IMAGE[:TAG]
# Evidence: .local/smoke/<timestamp>/ (env without secrets, check log, drill result).
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=ci-smoke
image=${1:?usage: smoke.sh IMAGE:TAG}
repo=${image%:*} tag=${image##*:}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$REPO_ROOT/.local/smoke/$stamp
secrets=$work/secrets
mkdir -p "$secrets"
chmod 700 "$secrets"
for s in db_root_password admin_password metrics_token restic_password; do
	random_secret >"$secrets/$s"
done
chmod 600 "$secrets"/*
env_file=$work/smoke.env
cat >"$env_file" <<EOF
COMPOSE_PROJECT_NAME=hinode-smoke
ENVIRONMENT=staging
SITE_NAME=smoke.localhost
SITES_RULE=Host(\`smoke.localhost\`)
TLS_MODE=none
HTTP_PUBLISH_PORT=127.0.0.1:18090
CUSTOM_IMAGE=$repo
CUSTOM_TAG=$tag
PULL_POLICY=never
CLAMAV=0
MONITORING=0
DB_PASSWORD_SECRETS_FILE=$secrets/db_root_password
ADMIN_PASSWORD_FILE=$secrets/admin_password
METRICS_TOKEN_FILE=$secrets/metrics_token
RESTIC_PASSWORD_FILE=$secrets/restic_password
RESTIC_REPOSITORY=/home/frappe/frappe-bench/sites/.restic-smoke
GUNICORN_WORKERS=2
BACKEND_MEM_LIMIT=1536m
BACKEND_CPUS=2
WORKER_MEM_LIMIT=1g
WORKER_CPUS=1
DB_BUFFER_POOL=256M
DB_MEM_LIMIT=1536m
DB_CPUS=2
EOF
compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$env_file")
teardown() {
	"${compose[@]}" logs --no-color >"$work/compose.log" 2>&1 || true
	"${compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
	grep -v -i 'password\|token\|secret' "$env_file" >"$work/smoke.env.redacted" 2>/dev/null || true
	rm -rf "$secrets" "$env_file"
}
trap teardown EXIT

log "starting stack ($image)"
"${compose[@]}" up -d --quiet-pull 2>&1 | tail -3
for _ in $(seq 1 60); do
	"${compose[@]}" exec -T db healthcheck.sh --connect >/dev/null 2>&1 && break
	sleep 5
done
"$REPO_ROOT/scripts/deploy/new-site.sh" "$env_file" 2>&1 | tail -5
"${compose[@]}" restart backend frontend >/dev/null
for _ in $(seq 1 30); do
	"${compose[@]}" exec -T frontend curl -fsS -o /dev/null -H "Host: smoke.localhost" \
		http://127.0.0.1:8080/api/method/automotive_core.ops.health.live 2>/dev/null && break
	sleep 5
done
"$REPO_ROOT/scripts/deploy/smoke.sh" "$env_file" 2>&1 | tee "$work/checks.log"
log "backup into a local restic repository and restore drill"
"${compose[@]}" exec -T scheduler bash /opt/hinode/ops/backup/backup.sh 2>&1 | tail -3 | tee -a "$work/checks.log"
"${compose[@]}" exec -T scheduler bash /opt/hinode/ops/backup/restore-drill.sh 2>&1 | tail -3 | tee -a "$work/checks.log"
"${compose[@]}" exec -T scheduler bash -c 'cat "$(ls -t sites/smoke.localhost/private/ops/drills/*.json | head -1)"' \
	>"$work/restore-drill.json"
log "smoke test passed; evidence in ${work#"$REPO_ROOT"/}"
