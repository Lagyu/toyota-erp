#!/usr/bin/env bash
# Runs INSIDE the development bench container (user frappe); see scripts/dev/up.sh.
# Idempotent and resumable: every finished step leaves a marker, a failed step is
# redone from scratch on the next run.
#
#   1. bench init with frappe at the pinned commit
#   2. erpnext at the pinned commit
#   3. custom apps from /workspace/apps symlinked and pip-installed (editable)
#   4. common_site_config pointing at the compose services
#   5. development and test sites with every app installed and migrated
set -euo pipefail

WS=/workspace
STATE=/home/frappe/dev
BENCH=$STATE/frappe-bench
MARKERS=$STATE/markers
export LOG_TAG=bootstrap
# shellcheck source=../lib/common.sh
source "$WS/scripts/lib/common.sh"
# a failing step leaves the ERROR line waiters look for (scripts/lib/wait-for-log.sh);
# die() logs one itself
set -E
trap 'log "ERROR: bootstrap failed (line $LINENO, exit $?)"' ERR
load_env_file "$WS/infra/versions.env"

SITE=${DEV_SITE:-erp.localhost}
TEST_SITE=${DEV_TEST_SITE:-test.localhost}
# Install order matters: each app depends on the ones before it.
CUSTOM_APPS=(automotive_core automotive_manufacturing automotive_supply_chain automotive_portals automotive_seed)
PY=$HOME/.pyenv/versions/$PYTHON_VERSION/bin/python3
[[ -x $PY ]] || die "python $PYTHON_VERSION not found in pyenv"
mkdir -p "$MARKERS"

if [[ ! -f $MARKERS/frappe-init.done ]]; then
	log "bench init: frappe $FRAPPE_REF"
	rm -rf "$BENCH"
	(cd "$STATE" && bench init --frappe-path "$FRAPPE_REPO" --frappe-branch "$FRAPPE_REF" \
		--python "$PY" --skip-redis-config-generation --no-backups --verbose frappe-bench)
	"$WS/scripts/lib/verify-checkout.sh" "$BENCH/apps/frappe" "$FRAPPE_COMMIT"
	touch "$MARKERS/frappe-init.done"
fi
"$WS/scripts/lib/verify-checkout.sh" "$BENCH/apps/frappe" "$FRAPPE_COMMIT"
cd "$BENCH"

if [[ ! -f $MARKERS/erpnext.done ]]; then
	log "get-app erpnext $ERPNEXT_REF"
	rm -rf apps/erpnext
	sed -i '/^erpnext$/d' sites/apps.txt
	bench get-app --branch "$ERPNEXT_REF" erpnext "$ERPNEXT_REPO"
	"$WS/scripts/lib/verify-checkout.sh" apps/erpnext "$ERPNEXT_COMMIT"
	touch "$MARKERS/erpnext.done"
fi
"$WS/scripts/lib/verify-checkout.sh" apps/erpnext "$ERPNEXT_COMMIT"

log "common_site_config"
bench set-config -g db_host mariadb
bench set-config -gp db_port 3306
bench set-config -g redis_cache redis://redis-cache:6379
bench set-config -g redis_queue redis://redis-queue:6379
bench set-config -g redis_socketio redis://redis-queue:6379
bench set-config -gp socketio_port 9000
bench set-config -gp webserver_port 8000
for chromium in /usr/lib/chromium/chromium-headless-shell /usr/lib/chromium/chromium; do
	if [[ -x $chromium ]]; then bench set-config -g chromium_path "$chromium"; break; fi
done

linked=()
for app in "${CUSTOM_APPS[@]}"; do
	[[ -f $WS/apps/$app/pyproject.toml ]] || continue
	ln -sfn "$WS/apps/$app" "apps/$app"
	if ! ./env/bin/python -c "import $app" 2>/dev/null; then
		log "pip install -e $app"
		./env/bin/python -m pip install --quiet -e "apps/$app"
	fi
	if ! grep -qxF "$app" sites/apps.txt; then
		# apps.txt written by bench has no trailing newline
		[[ -s sites/apps.txt && -n $(tail -c1 sites/apps.txt) ]] && echo >>sites/apps.txt
		echo "$app" >>sites/apps.txt
	fi
	linked+=("$app")
done

ensure_site() {
	local site=$1
	if [[ ! -f $MARKERS/site-$site.done ]]; then
		log "new-site $site"
		bench drop-site "$site" --db-root-username root --db-root-password "$DEV_DB_ROOT_PASSWORD" \
			--force --no-backup >/dev/null 2>&1 || true
		rm -rf "sites/$site"
		bench new-site "$site" --db-root-username root --db-root-password "$DEV_DB_ROOT_PASSWORD" \
			--admin-password "$DEV_ADMIN_PASSWORD" --mariadb-user-host-login-scope='%' \
			--install-app erpnext
		touch "$MARKERS/site-$site.done"
	fi
	local installed
	installed=$(bench --site "$site" list-apps --format json 2>/dev/null | python3 -c \
		'import json,sys; d=json.load(sys.stdin); print(" ".join(next(iter(d.values()))))' 2>/dev/null || true)
	for app in "${linked[@]}"; do
		if [[ " $installed " != *" $app "* ]]; then
			log "install-app $app on $site"
			bench --site "$site" install-app "$app"
		fi
	done
}

ensure_site "$SITE"
bench --site "$SITE" set-config -p developer_mode 1
bench --site "$SITE" set-config -p allow_tests 0
bench use "$SITE"

ensure_site "$TEST_SITE"
bench --site "$TEST_SITE" set-config -p allow_tests 1
bench --site "$TEST_SITE" set-config -p developer_mode 0

for site in "$SITE" "$TEST_SITE"; do
	log "migrate $site"
	bench --site "$site" migrate
done

if ((${#linked[@]})); then
	log "build assets: ${linked[*]}"
	bench build --apps "$(
		IFS=,
		echo "${linked[*]}"
	)"
fi

log "BOOTSTRAP OK (apps: frappe erpnext ${linked[*]})"
