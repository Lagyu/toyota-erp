# shellcheck shell=bash
# Reference stacks for CI jobs (synthetic dataset segments, performance tests): the
# production compose (scripts/deploy/compose.sh) from a reference image, with secrets
# generated for the stack, one site, and restore/export of a dataset. Source after
# common.sh:
#   stack_init NAME HTTP_PORT IMAGE:TAG [KEY=VALUE ...]   environment overrides (limits)
#   stack_up                     start, wait for the database, create and configure the site
#   stack_restore DIR            load DIR/{database.sql.gz,public.tgz,private.tgz,encryption_key}
#   stack_export DIR             write the same files from the site
#   stack_bench ARGS...          bench --site SITE ARGS in the backend container
#   stack_down LOG_FILE          save the containers' logs, remove the stack and its secrets
# Variables after stack_init: stack_site, stack_url, stack_secrets, stack_compose (array).

# shellcheck disable=SC2034  # set by stack_init, read by the scripts that source this file
stack_site="" stack_url="" stack_secrets="" stack_env="" stack_work="" stack_backups=""
stack_compose=()

stack_init() {
	local name=${1:?name} port=${2:?port} image=${3:?image}
	shift 3
	stack_work=$REPO_ROOT/.local/$name-stack/$(date -u +%Y%m%dT%H%M%SZ)
	stack_secrets=$stack_work/secrets
	stack_site=$name.localhost
	stack_url=http://$stack_site:$port
	mkdir -p "$stack_secrets"
	chmod 700 "$stack_secrets"
	(umask 077 && for s in db_root_password admin_password metrics_token restic_password; do
		random_secret >"$stack_secrets/$s"
	done)
	secret_for_frappe "$stack_secrets/db_root_password" "$stack_secrets/restic_password"
	stack_env=$stack_work/$name.env
	cat >"$stack_env" <<EOF
COMPOSE_PROJECT_NAME=hinode-$name
ENVIRONMENT=reference
SITE_NAME=$stack_site
SITES_RULE=Host(\`$stack_site\`)
TLS_MODE=none
HTTP_PUBLISH_PORT=127.0.0.1:$port
CUSTOM_IMAGE=${image%:*}
CUSTOM_TAG=${image##*:}
PULL_POLICY=never
CLAMAV=0
MONITORING=0
DB_PASSWORD_SECRETS_FILE=$stack_secrets/db_root_password
ADMIN_PASSWORD_FILE=$stack_secrets/admin_password
METRICS_TOKEN_FILE=$stack_secrets/metrics_token
RESTIC_PASSWORD_FILE=$stack_secrets/restic_password
RESTIC_REPOSITORY=/home/frappe/frappe-bench/sites/.restic-$name
BACKUP_SCHEDULE=off
RESTORE_DRILL_SCHEDULE=off
BINLOG_SCHEDULE=off
DB_BINLOG_RETENTION_SECONDS=3600
EOF
	# (above) scheduled backups, drills and binary log shipping would compete with the generator
	# or the load; binary logs older than an hour are purged so a long generation fits the disk
	printf '%s\n' "$@" >>"$stack_env"
	stack_compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$stack_env")
	stack_backups=/home/frappe/frappe-bench/sites/$stack_site/private/backups
}

stack_bench() { "${stack_compose[@]}" exec -T backend bench --site "$stack_site" "$@"; }

stack_up() {
	log "starting the $stack_site stack"
	"${stack_compose[@]}" up -d --quiet-pull 2>&1 | tail -3
	local _
	for _ in $(seq 1 60); do
		"${stack_compose[@]}" exec -T db healthcheck.sh --connect >/dev/null 2>&1 && break
		sleep 5
	done
	"$REPO_ROOT/scripts/deploy/new-site.sh" "$stack_env" 2>&1 | tail -3
}

stack_restore() {
	local dir=${1:?dataset directory} f
	log "restoring the dataset from ${dir#"$REPO_ROOT"/}"
	# staged on the sites volume: the container's /tmp is a small tmpfs
	for f in database.sql.gz public.tgz private.tgz; do
		"${stack_compose[@]}" exec -T backend bash -c "cat >'$stack_backups/restore-$f'" <"$dir/$f"
	done
	"${stack_compose[@]}" exec -T -e DB_ROOT="$(<"$stack_secrets/db_root_password")" backend bash -c \
		"bench --site '$stack_site' restore '$stack_backups/restore-database.sql.gz' \
		--with-public-files '$stack_backups/restore-public.tgz' --with-private-files '$stack_backups/restore-private.tgz' \
		--db-root-password \"\$DB_ROOT\" --force | tail -3 && rm -f '$stack_backups'/restore-*"
	# encrypted fields stay readable with the dataset's own key
	if [[ -s $dir/encryption_key ]]; then
		"${stack_compose[@]}" exec -T backend python3 -c "$SET_SITE_CONFIG_PY" "$stack_site" encryption_key \
			<"$dir/encryption_key"
	fi
	stack_bench migrate >/dev/null
	# the dataset carries the Administrator password of the stack that generated it
	"${stack_compose[@]}" exec -T backend env/bin/python -c "$SET_ADMIN_PASSWORD_PY" "$stack_site" \
		<"$stack_secrets/admin_password"
}

stack_export() {
	local dir=${1:?output directory} public
	mkdir -p "$dir"
	log "exporting the site to ${dir#"$REPO_ROOT"/}"
	stack_bench backup --with-files --compress >/dev/null
	newest() { "${stack_compose[@]}" exec -T backend bash -c "ls -t $stack_backups/*$1 | head -1"; }
	# trigger DEFINERs name this stack's database user: strip them so any site loads the dump
	"${stack_compose[@]}" exec -T backend bash -c "zcat '$(newest -database.sql.gz)' \
		| sed -E 's#/\\*!50017 DEFINER=\`[^\`]*\`@\`[^\`]*\`\\*/ ?##g' | gzip -c" >"$dir/database.sql.gz"
	"${stack_compose[@]}" cp "backend:$(newest -private-files.tgz)" "$dir/private.tgz" >/dev/null
	public=$("${stack_compose[@]}" exec -T backend bash -c "ls -t $stack_backups/*-files.tgz | grep -v private-files | head -1")
	"${stack_compose[@]}" cp "backend:$public" "$dir/public.tgz" >/dev/null
	"${stack_compose[@]}" exec -T backend python3 -c \
		"import json; print(json.load(open('sites/$stack_site/site_config.json')).get('encryption_key', ''))" \
		>"$dir/encryption_key"
	chmod 600 "$dir/encryption_key"
}

stack_down() {
	local log_file=${1:-}
	if [[ -n $log_file ]]; then
		mkdir -p "$(dirname "$log_file")"
		"${stack_compose[@]}" logs --no-color >"$log_file" 2>&1 || true
	fi
	"${stack_compose[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
	rm -rf "$stack_secrets" "$stack_env"
}
