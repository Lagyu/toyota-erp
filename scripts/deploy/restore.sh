#!/usr/bin/env bash
# Disaster recovery: restore a restic snapshot (default: newest) over the environment's
# site. Destructive; asks for the site name as confirmation.
#   restore.sh ENV_FILE [SNAPSHOT_ID]
# Steps: maintenance on -> restic restore -> strip dump DEFINERs -> bench restore with
# files -> migrate -> maintenance off -> smoke test. See ops/runbooks/backup-restore.md.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=restore
env_file=${1:?usage: restore.sh ENV_FILE [SNAPSHOT]}
snapshot=${2:-latest}
load_env_file "$env_file"
compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$env_file")
site=${SITE_NAME:?}
read -r -p "This replaces ALL data of $site with snapshot $snapshot. Type the site name to continue: " answer
[[ $answer == "$site" ]] || die "aborted"

"${compose[@]}" exec -T backend bench --site "$site" set-maintenance-mode on
"${compose[@]}" exec -T -e SNAP="$snapshot" scheduler bash -c '
	set -euo pipefail
	cd /home/frappe/frappe-bench
	work=$(mktemp -d)
	if [ "$SNAP" = latest ]; then restic restore latest --host "$SITE_NAME" --target "$work";
	else restic restore "$SNAP" --target "$work"; fi
	db=$(find "$work" -name "*-database.sql.gz" | head -1)
	zcat "$db" | sed -E "s#/\*!50017 DEFINER=\`[^\`]*\`@\`[^\`]*\`\*/ ?##g" | gzip -c > "$work/restore.sql.gz"
	bench --site "$SITE_NAME" restore "$work/restore.sql.gz" \
		--with-public-files "$(find "$work" -name "*-files.tar" | head -1)" \
		--with-private-files "$(find "$work" -name "*-private-files.tar" | head -1)" \
		--db-root-password "$(cat "$DB_ROOT_PASSWORD_FILE")" --force
	rm -rf "$work"'
"${compose[@]}" exec -T backend bench --site "$site" migrate
"${compose[@]}" exec -T backend bench --site "$site" set-maintenance-mode off
"$REPO_ROOT/scripts/deploy/smoke.sh" "$env_file"
log "restored $site from $snapshot"
