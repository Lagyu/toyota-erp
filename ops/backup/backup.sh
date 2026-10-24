#!/usr/bin/env bash
# Nightly backup of the site into the off-host restic repository (runs in the scheduler
# container, scheduled by ofelia; also called by scripts/deploy/upgrade.sh).
#
#   1. bench backup --with-files (database dump, public + private files, site config)
#   2. restic backup (client-side AES-256 encryption; the key never leaves this host)
#   3. retention (daily/weekly/monthly) and a 5% read-back integrity check
#   4. marker for the erp_backup_last_success_timestamp_seconds metric (in private/ops: bench
#      backup deletes everything in private/backups older than keep_backups_for_hours)
# Environment: SITE_NAME, RESTIC_REPOSITORY, RESTIC_PASSWORD_FILE, RESTIC_KEEP_*,
# and the repository's credentials (e.g. AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY).
set -euo pipefail
site=${SITE_NAME:?}
cd /home/frappe/frappe-bench
dir="sites/$site/private/backups"
log() { printf '%s [backup] %s\n' "$(date -u +%FT%TZ)" "$*"; }

log "bench backup $site"
bench --site "$site" backup --with-files --compress >/dev/null
db=$(ls -t "$dir"/*-database.sql.gz | head -1)
prefix=${db%-database.sql.gz}
set_files=("$prefix"*)
log "backup set: ${set_files[*]##*/}"

restic cat config >/dev/null 2>&1 || { log "initialising repository"; restic init; }
restic backup --quiet --host "$site" --tag "site:$site" --tag "${BACKUP_TAG:-nightly}" "${set_files[@]}"
# group by host and tags: every snapshot holds differently named files, so restic's default
# grouping (host, paths) would make each snapshot its own group and never forget any
restic forget --quiet --host "$site" --tag "site:$site" --group-by host,tags --prune \
	--keep-daily "${RESTIC_KEEP_DAILY:-14}" --keep-weekly "${RESTIC_KEEP_WEEKLY:-8}" \
	--keep-monthly "${RESTIC_KEEP_MONTHLY:-12}"
restic check --read-data-subset=5% --quiet

mkdir -p "sites/$site/private/ops"
date -u +%FT%TZ >"sites/$site/private/ops/backup-last-success.txt"
# keep three days of local copies for quick restores; the repository holds the rest
find "$dir" -maxdepth 1 -type f \( -name '*.sql.gz' -o -name '*.tar' -o -name '*.tgz' -o -name '*.json' \) \
	-mtime +3 -delete
log "done: snapshot $(restic snapshots --host "$site" --latest 1 --json | python3 -c \
	'import json, sys; s = json.load(sys.stdin)[-1]; print(s["short_id"], s["time"][:19])')"
