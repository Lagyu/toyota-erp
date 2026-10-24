#!/usr/bin/env bash
# Disaster recovery: restore a backup set (default: the newest) over the environment's site
# and bring it forward with the shipped binary logs (point-in-time recovery), to the newest
# shipped change or to --until. Destructive; asks for the site name as confirmation.
#   restore.sh ENV_FILE [SNAPSHOT_ID] [--until "YYYY-MM-DD HH:MM:SS" (UTC) | --no-replay]
# Steps: maintenance on -> restic restore -> strip dump DEFINERs -> bench restore with
# files -> binary log replay -> migrate -> maintenance off -> smoke test. If the replay
# fails, the site holds the backup set and stays in maintenance mode: run again with
# --no-replay to accept the backup's point, or investigate. See ops/runbooks/backup-restore.md.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=restore
env_file=${1:?usage: restore.sh ENV_FILE [SNAPSHOT] [--until TIME | --no-replay]}
shift
snapshot=latest until="" replay=1
while [[ $# -gt 0 ]]; do
	case $1 in
		--until) until=${2:?--until needs a time (UTC)}; shift 2 ;;
		--no-replay) replay=0; shift ;;
		-*) die "unknown option $1" ;;
		*) snapshot=$1; shift ;;
	esac
done
load_env_file "$env_file"
compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$env_file")
site=${SITE_NAME:?}
if [[ $replay == 1 ]]; then
	what="backup set $snapshot, brought forward to ${until:-the newest shipped change} (UTC)"
else
	what="backup set $snapshot as it is (no binary log replay)"
fi
read -r -p "This replaces ALL data of $site with the $what. Type the site name to continue: " answer
[[ $answer == "$site" ]] || die "aborted"

"${compose[@]}" exec -T backend bench --site "$site" set-maintenance-mode on
"${compose[@]}" exec -T -e SNAP="$snapshot" -e REPLAY="$replay" -e UNTIL="$until" scheduler bash -c '
	set -euo pipefail
	cd /home/frappe/frappe-bench
	work=$(mktemp -d)
	# the newest backup set: binary log snapshots carry the tag binlog:<site> instead
	if [ "$SNAP" = latest ]; then restic restore latest --host "$SITE_NAME" --tag "site:$SITE_NAME" --target "$work";
	else restic restore "$SNAP" --target "$work"; fi
	db=$(find "$work" -name "*-database.sql.gz" | head -1)
	zcat "$db" | sed -E "s#/\*!50017 DEFINER=\`[^\`]*\`@\`[^\`]*\`\*/ ?##g" | gzip -c > "$work/restore.sql.gz"
	# bench backup --compress writes .tgz archives (.tar without it); public must not match private
	public=$(find "$work" \( -name "*-files.tar" -o -name "*-files.tgz" \) ! -name "*-private-files.*" | head -1)
	private=$(find "$work" \( -name "*-private-files.tar" -o -name "*-private-files.tgz" \) | head -1)
	if [ -z "$public" ] || [ -z "$private" ]; then echo "the snapshot holds no file archives" >&2; exit 1; fi
	bench --site "$SITE_NAME" restore "$work/restore.sql.gz" \
		--with-public-files "$public" --with-private-files "$private" \
		--db-root-password "$(cat "$DB_ROOT_PASSWORD_FILE")" --force
	# on a new host the site has a key of its own: take the source key so secrets decrypt
	config=$(find "$work" -name "*-site_config_backup.json" | head -1)
	if [ -n "$config" ]; then
		python3 /opt/hinode/ops/backup/adopt_encryption_key.py "$config" "sites/$SITE_NAME/site_config.json"
	fi
	if [ "$REPLAY" = 1 ]; then
		bash /opt/hinode/ops/backup/replay.sh "$work" "$SITE_NAME" "$work/pitr.json" "$UNTIL"
		python3 -c "import json, sys; print(json.dumps(json.load(open(sys.argv[1]))))" "$work/pitr.json"
	fi
	rm -rf "$work"'
"${compose[@]}" exec -T backend bench --site "$site" migrate
"${compose[@]}" exec -T backend bench --site "$site" set-maintenance-mode off
wait_ready "$env_file" "$site" 61 || true  # the smoke test below reports what is wrong
"$REPO_ROOT/scripts/deploy/smoke.sh" "$env_file"
log "restored $site from $snapshot"
