#!/usr/bin/env bash
# Create the environment's site on a running stack and configure it for its role.
#   new-site.sh ENV_FILE
# Installs ERPNext and the automotive apps (plus automotive_seed on reference images),
# applies the Hinode tenant configuration, sets the metrics token and enables the
# scheduler. Idempotent: an existing site is only re-configured.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=new-site
env_file=${1:?usage: new-site.sh ENV_FILE}
load_env_file "$env_file"
compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$env_file")
site=${SITE_NAME:?}
db_root=$(<"${DB_PASSWORD_SECRETS_FILE:?}")
admin=$(<"${ADMIN_PASSWORD_FILE:?}")
token=$(<"${METRICS_TOKEN_FILE:?}")

bench() { "${compose[@]}" exec -T backend bench "$@"; }

if ! "${compose[@]}" exec -T backend test -f "sites/$site/site_config.json"; then
	log "creating $site"
	"${compose[@]}" exec -T -e DB_ROOT="$db_root" -e ADMIN="$admin" backend bash -c \
		"bench new-site '$site' --mariadb-user-host-login-scope='%' --db-root-password \"\$DB_ROOT\" \
		--admin-password \"\$ADMIN\" --install-app erpnext"
fi
# an encryption key from the start (lib/common.sh: two processes creating one at once lose data)
"${compose[@]}" exec -T backend env/bin/python -c "$ENSURE_ENCRYPTION_KEY_PY" "$site"
installed=$(bench --site "$site" list-apps)
for app in automotive_core automotive_manufacturing automotive_supply_chain automotive_portals; do
	grep -qw "$app" <<<"$installed" || bench --site "$site" install-app "$app"
done
if [[ ${ENVIRONMENT:?} == reference ]]; then
	grep -qw automotive_seed <<<"$installed" || bench --site "$site" install-app automotive_seed
	bench --site "$site" set-config -p automotive_allow_seed 1
fi
bench --site "$site" set-config automotive_environment "$ENVIRONMENT"
# bench backup prunes local backup files older than this; ops/backup/backup.sh keeps three days
bench --site "$site" set-config -p keep_backups_for_hours 72
bench --site "$site" set-config host_name "https://$site"
"${compose[@]}" exec -T backend python3 -c "$SET_SITE_CONFIG_PY" "$site" automotive_metrics_token <<<"$token"
# the tenant setup returns its change log (one JSON line); report its size, keep the detail
steps=$(bench --site "$site" execute automotive_core.setup.tenant.apply --kwargs "{'tenant': 'hinode'}" | tail -1)
log "tenant setup: $(python3 -c 'import json, sys; print(len(json.loads(sys.argv[1] or "[]")), "changes")' "$steps" \
	2>/dev/null || echo "applied")"
bench --site "$site" enable-scheduler
bench use "$site"
log "site $site ready ($ENVIRONMENT)"
