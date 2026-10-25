#!/usr/bin/env bash
# The MariaDB account mysqld-exporter signs in with (MONITORING=1), from the credentials in
# MYSQLD_EXPORTER_CNF: created, or brought to that file's password, with the rights the
# exporter's collectors need and no access to the ERP's data (PROCESS, REPLICATION CLIENT and
# SLAVE MONITOR; SELECT on performance_schema only). Then mysqld-exporter restarts to sign in
# with it. Run after the first start (the database must be up), and again to rotate the
# exporter's password: put a new one in the file first (ops/runbooks/security-operations.md).
#   monitoring-account.sh ENV_FILE
# The root password reaches the backend container on stdin, the exporter's too; neither is
# printed. Works with both datastore options (the backend reaches the database either way).
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=monitoring-account
env_file=${1:?usage: monitoring-account.sh ENV_FILE}
load_env_file "$env_file"
compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$env_file")
cnf=${MYSQLD_EXPORTER_CNF:?MYSQLD_EXPORTER_CNF is not set (the monitoring overlay, MONITORING=1)}
[[ -r $cnf ]] || die "$cnf: the deploying user cannot read it (chown 65534:<deploy group>, chmod 0440)"
user=$(sed -n 's/^user *= *//p' "$cnf" | head -1)
[[ -n $user ]] && grep -q '^password *= *.' "$cnf" || die "$cnf needs [client] user=... and password=..."

# Python for the backend container (working directory: the bench); stdin: the database
# administrator's password, then the exporter's
ACCOUNT_PY='import json, sys
import pymysql
user, admin = sys.argv[1:3]
root_password, password = sys.stdin.read().split("\n")[:2]
common = json.load(open("sites/common_site_config.json"))
conn = pymysql.connect(host=common["db_host"], port=int(common.get("db_port") or 3306), user=admin,
	password=root_password)
account = (user, "%")
with conn.cursor() as cur:
	cur.execute("SELECT COUNT(*) FROM mysql.user WHERE user = %s AND host = %s", account)
	existed = cur.fetchone()[0]
	cur.execute("CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s", (*account, password))
	cur.execute("ALTER USER %s@%s IDENTIFIED BY %s WITH MAX_USER_CONNECTIONS 3", (*account, password))
	cur.execute("REVOKE ALL PRIVILEGES, GRANT OPTION FROM %s@%s", account)
	cur.execute("GRANT PROCESS, REPLICATION CLIENT, SLAVE MONITOR ON *.* TO %s@%s", account)
	cur.execute("GRANT SELECT ON performance_schema.* TO %s@%s", account)
conn.commit()
print(("password and rights set for" if existed else "created") + f" {user}@%")'

# printf is a builtin: the passwords are on no command line
result=$(printf '%s\n%s\n' "$(<"${DB_PASSWORD_SECRETS_FILE:?}")" "$(sed -n 's/^password *= *//p' "$cnf" | head -1)" \
	| "${compose[@]}" exec -T backend env/bin/python -c "$ACCOUNT_PY" "$user" "${DB_ROOT_USER:-root}")
log "$result"
if "${compose[@]}" ps --services --status running | grep -qx mysqld-exporter; then
	"${compose[@]}" restart mysqld-exporter >/dev/null
	log "mysqld-exporter restarted"
fi
