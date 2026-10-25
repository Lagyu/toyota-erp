#!/usr/bin/env bash
# Rotate the site's own database password (the account the application connects with; the
# root password is a separate procedure: ops/runbooks/security-operations.md).
#   rotate-site-db-password.sh ENV_FILE
# 1. maintenance mode on: requests answer 503 instead of failing their connection
# 2. a new random password for the site's database account, on every host the account has,
#    and in the site config, in one step in the backend container (the database root password
#    and the new one reach it on stdin, never on a command line; DATASTORES=external included)
# 3. the processes that hold database connections restarted with the new config, then the
#    front end (it resolves the backend's address when it starts)
# 4. maintenance mode off, wait until every worker serves it, smoke test
# The old password stops working at step 2. The new one is in the site config and in every
# later backup set (*-site_config_backup.json): nothing else needs it. The high-availability
# option's other application hosts read the shared site config, but their running processes
# keep the old connections' settings: restart their backend, workers and front end too.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=rotate-db
env_file=${1:?usage: rotate-site-db-password.sh ENV_FILE}
load_env_file "$env_file"
compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$env_file")
site=${SITE_NAME:?}

# Python for the backend container (working directory: the bench): stdin holds the database
# administrator's password, then the new one; prints the account and the hosts changed, never
# a password
ROTATE_PY='import json, os, sys
import pymysql
site, admin = sys.argv[1:3]
root_password, new_password = sys.stdin.read().split("\n")[:2]
common = json.load(open("sites/common_site_config.json"))
path = os.path.join("sites", site, "site_config.json")
config = json.load(open(path))
user = config.get("db_user") or config["db_name"]
conn = pymysql.connect(host=config.get("db_host") or common.get("db_host"),
	port=int(config.get("db_port") or common.get("db_port") or 3306), user=admin, password=root_password)
with conn.cursor() as cur:
	cur.execute("SELECT host FROM mysql.user WHERE user = %s", (user,))
	hosts = [row[0] for row in cur.fetchall()]
	if not hosts:
		sys.exit(f"no database account {user}")
	for host in hosts:
		cur.execute("ALTER USER %s@%s IDENTIFIED BY %s", (user, host, new_password))
conn.commit()
conn.close()
config["db_password"] = new_password
tmp = path + ".tmp"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
	json.dump(config, handle, indent=1, sort_keys=True)
os.replace(tmp, path)
print(user + "@" + ",".join(hosts))'

"${compose[@]}" exec -T backend bench --site "$site" set-maintenance-mode on
new_password=$(random_secret)
account=$(printf '%s\n%s\n' "$(<"${DB_PASSWORD_SECRETS_FILE:?}")" "$new_password" \
	| "${compose[@]}" exec -T backend env/bin/python -c "$ROTATE_PY" "$site" "${DB_ROOT_USER:-root}")
unset new_password
log "database account $account: new password set in MariaDB and in the site config"
# every process that holds database connections reads the new config on start
running=$("${compose[@]}" ps --services --status running)
restart=()
for service in backend queue-short queue-long scheduler cron; do
	grep -qx "$service" <<<"$running" && restart+=("$service")
done
"${compose[@]}" restart "${restart[@]}" >/dev/null
# the front end (nginx) resolves the backend's address when it starts, and containers restarted
# together can come back with each other's addresses: it starts again after them
if grep -qx frontend <<<"$running"; then
	"${compose[@]}" restart frontend >/dev/null
	restart+=(frontend)
fi
log "restarted: ${restart[*]}"
"${compose[@]}" exec -T backend bench --site "$site" set-maintenance-mode off
wait_ready "$env_file" "$site" 61 || true  # the smoke test below reports what is wrong
"$REPO_ROOT/scripts/deploy/smoke.sh" "$env_file"
log "rotated the database password of $site"
