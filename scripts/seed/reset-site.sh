#!/usr/bin/env bash
# Runs INSIDE the bench container: put a reference site back to "apps installed, no data".
# The first call builds the site and keeps a snapshot of that state; later calls restore
# the snapshot (much faster than reinstalling ERPNext) and migrate it to the current code.
#   reset-site.sh SITE [--rebuild]
set -euo pipefail
site=${1:?site name}
rebuild=${2:-}
bench_dir=/home/frappe/dev/frappe-bench
base_dir=/home/frappe/dev/seed-base
base=$base_dir/$site.sql.gz
cd "$bench_dir"
mkdir -p "$base_dir"

if [[ $rebuild == --rebuild || ! -f $base ]]; then
	if [[ -f sites/$site/site_config.json ]]; then
		bench drop-site "$site" --db-root-username root --db-root-password "$DEV_DB_ROOT_PASSWORD" --force --no-backup
	fi
	/workspace/scripts/seed/create-site.sh "$site"
	bench --site "$site" backup --backup-path "$base_dir/tmp-$site"
	/workspace/scripts/lib/strip-definers.sh "$(ls -t "$base_dir/tmp-$site"/*-database.sql.gz | head -1)" "$base"
	rm -rf "$base_dir/tmp-$site"
else
	bench --site "$site" --force restore "$base" --db-root-username root --db-root-password "$DEV_DB_ROOT_PASSWORD"
	bench --site "$site" set-config -p automotive_allow_seed 1
	bench --site "$site" set-config automotive_environment reference
	bench --site "$site" migrate
fi
rm -rf "sites/$site/private/seed" "sites/$site/private/integration"
echo "SITE RESET $site"
