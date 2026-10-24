#!/usr/bin/env bash
# Roll the stack to the image in ENV_FILE (CUSTOM_TAG) with a verified backup first.
#   upgrade.sh ENV_FILE
# 1. maintenance mode on   2. backup (restic, off-host) of the running version: taken inside
# the maintenance window, it is exactly the state being upgraded, the rollback point
# 3. pull/recreate on the new image   4. bench migrate   5. maintenance off, wait until
# ready   6. smoke test
# A failed migrate leaves maintenance mode ON; recover with ops/runbooks/upgrade.md
# (redeploy the previous CUSTOM_TAG, restore the step-2 snapshot with --no-replay).
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=upgrade
env_file=${1:?usage: upgrade.sh ENV_FILE}
load_env_file "$env_file"
compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$env_file")
site=${SITE_NAME:?}

"${compose[@]}" exec -T backend bench --site "$site" set-maintenance-mode on
log "pre-upgrade backup (the rollback point)"
# tagged pre-upgrade: restic forget groups snapshots by host and tags, so the nightly backups'
# retention (one snapshot a day) cannot replace the rollback point with a later backup that day
"${compose[@]}" exec -T -e BACKUP_TAG=pre-upgrade scheduler bash /opt/hinode/ops/backup/backup.sh
"${compose[@]}" pull --quiet || true
"${compose[@]}" up -d --remove-orphans
"${compose[@]}" exec -T backend bench --site "$site" migrate
# sites created before new-site.sh did this get their encryption key now, while the workers
# refuse requests; the wait below lets every worker read it (lib/common.sh)
"${compose[@]}" exec -T backend env/bin/python -c "$ENSURE_ENCRYPTION_KEY_PY" "$site"
"${compose[@]}" exec -T backend bench --site "$site" set-maintenance-mode off
wait_ready "$env_file" "$site" 61 || true  # the smoke test below reports what is wrong
"$REPO_ROOT/scripts/deploy/smoke.sh" "$env_file"
log "upgraded $site to ${CUSTOM_TAG}"
