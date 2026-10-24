#!/usr/bin/env bash
# Runs INSIDE the bench container: create a reference (non-production) site with all apps
# and synthetic data generation enabled. Idempotent.
#   create-site.sh SITE
set -euo pipefail
site=${1:?site name}
cd /home/frappe/dev/frappe-bench
if [[ ! -f sites/$site/site_config.json ]]; then
	bench new-site "$site" --db-root-username root --db-root-password "$DEV_DB_ROOT_PASSWORD" \
		--admin-password "$DEV_ADMIN_PASSWORD" --mariadb-user-host-login-scope='%' --install-app erpnext
fi
installed=$(bench --site "$site" list-apps --format json | python3 -c 'import json,sys; print(" ".join(next(iter(json.load(sys.stdin).values()))))')
for app in automotive_core automotive_manufacturing automotive_supply_chain automotive_portals automotive_seed; do
	[[ " $installed " == *" $app "* ]] || bench --site "$site" install-app "$app"
done
bench --site "$site" set-config -p automotive_allow_seed 1
bench --site "$site" set-config automotive_environment reference
bench --site "$site" migrate
echo "SITE READY $site"
