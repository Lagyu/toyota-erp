#!/usr/bin/env bash
# The statements that take the database's time, from MariaDB's statement digests
# (performance_schema, on in compose.datastores.yaml): normalised SQL without its values, with
# calls, times, rows and index use, counted since the server started or the last --reset.
# Read-only apart from --reset; prints a Markdown table (docs: ops/performance/capacity.md).
#   slow-queries.sh ENV_FILE [--by total|mean] [--limit N] [--reset]
# --by total (default): by summed time, the load on the server; --by mean: by mean time over
# statements run at least 10 times, what a user waits for. --reset empties the digests, to
# measure a window from now.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=slow-queries
env_file=${1:?usage: slow-queries.sh ENV_FILE [--by total|mean] [--limit N] [--reset]}
shift
by=total limit=20 reset=0
while (($#)); do
	case $1 in
	--by) by=${2:?--by total or mean}; shift 2 ;;
	--limit) limit=${2:?--limit N}; shift 2 ;;
	--reset) reset=1; shift ;;
	*) die "unknown option $1" ;;
	esac
done
[[ $by == total || $by == mean ]] || die "--by total or --by mean"
[[ $limit =~ ^[1-9][0-9]*$ ]] || die "--limit takes a positive number"
load_env_file "$env_file"
site=${SITE_NAME:?}

# Python for the backend container (working directory: the bench), which reaches the database
# with either datastore option; the administrator's password arrives on stdin
DIGEST_PY='import json, os, sys
import pymysql
site, admin, by, limit, reset = sys.argv[1:6]
password = sys.stdin.read().split("\n")[0]
common = json.load(open("sites/common_site_config.json"))
config = json.load(open(os.path.join("sites", site, "site_config.json")))
conn = pymysql.connect(host=config.get("db_host") or common.get("db_host"),
	port=int(config.get("db_port") or common.get("db_port") or 3306), user=admin, password=password)
with conn.cursor() as cur:
	cur.execute("SELECT @@performance_schema")
	if not cur.fetchone()[0]:
		sys.exit("performance_schema is off: the db service runs with --performance-schema=ON (compose.datastores.yaml)")
	if reset == "1":
		cur.execute("TRUNCATE performance_schema.events_statements_summary_by_digest")
		print("statement digests emptied: they count from now")
		raise SystemExit
	order, floor = ("SUM_TIMER_WAIT", 1) if by == "total" else ("AVG_TIMER_WAIT", 10)
	cur.execute("SELECT DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT / 1e12, AVG_TIMER_WAIT / 1e9, MAX_TIMER_WAIT / 1e9, "
		"SUM_ROWS_EXAMINED / COUNT_STAR, SUM_ROWS_SENT / COUNT_STAR, SUM_NO_INDEX_USED + SUM_NO_GOOD_INDEX_USED, LAST_SEEN "
		"FROM performance_schema.events_statements_summary_by_digest "
		f"WHERE SCHEMA_NAME = %s AND DIGEST_TEXT IS NOT NULL AND COUNT_STAR >= %s ORDER BY {order} DESC LIMIT %s",
		(config["db_name"], floor, int(limit)))
	rows = cur.fetchall()
print(f"Statements of {site} by {by} time (digests since the server start or the last reset):")
print()
print("| Statement (normalised) | Calls | Total s | Mean ms | Max ms | Rows examined | Rows sent | Without index | Last seen |")
print("|---|---|---|---|---|---|---|---|---|")
for text, calls, total, mean, peak, examined, sent, unindexed, last in rows:
	text = " ".join(text.split())[:220].replace("|", "\\|")
	print(f"| {text} | {calls} | {total:.1f} | {mean:.1f} | {peak:.1f} | {examined:.0f} | {sent:.0f} | {unindexed} | {last} |")'

"$REPO_ROOT/scripts/deploy/compose.sh" "$env_file" exec -T backend env/bin/python -c "$DIGEST_PY" \
	"$site" "${DB_ROOT_USER:-root}" "$by" "$limit" "$reset" <"${DB_PASSWORD_SECRETS_FILE:?}"
