#!/usr/bin/env bash
# Roll the stack to the image in ENV_FILE (CUSTOM_TAG) with a verified backup first.
#   upgrade.sh ENV_FILE
# 1. backup (restic, off-host) of the running version   2. maintenance mode on
# 3. pull/recreate on the new image   4. bench migrate   5. maintenance off, wait until
# ready   6. smoke test
# A failed migrate leaves maintenance mode ON; recover with ops/runbooks/upgrade.md
# (restore the step-1 snapshot, redeploy the previous CUSTOM_TAG).
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=upgrade
env_file=${1:?usage: upgrade.sh ENV_FILE}
load_env_file "$env_file"
compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$env_file")
site=${SITE_NAME:?}

log "pre-upgrade backup"
"${compose[@]}" exec -T scheduler bash /opt/hinode/ops/backup/backup.sh
"${compose[@]}" exec -T backend bench --site "$site" set-maintenance-mode on
"${compose[@]}" pull --quiet || true
"${compose[@]}" up -d --remove-orphans
"${compose[@]}" exec -T backend bench --site "$site" migrate
"${compose[@]}" exec -T backend bench --site "$site" set-maintenance-mode off
wait_ready "$env_file" "$site" 61 || true  # the smoke test below reports what is wrong
"$REPO_ROOT/scripts/deploy/smoke.sh" "$env_file"
log "upgraded $site to ${CUSTOM_TAG}"
