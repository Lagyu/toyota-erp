#!/usr/bin/env bash
# Runs INSIDE the bench container: generate (or resume) a profile on a site, logging to
# /home/frappe/dev/logs/seed-<site>.log. Re-running resumes from the last committed day.
#   run.sh SITE PROFILE [UNTIL_DATE]
set -euo pipefail
site=${1:?site} profile=${2:?profile} until=${3:-}
cd /home/frappe/dev/frappe-bench
kwargs="{'profile': '$profile'}"
[[ -n $until ]] && kwargs="{'profile': '$profile', 'until': '$until'}"
echo "$(date -u +%FT%TZ) seed start site=$site profile=$profile until=${until:-end}"
bench --site "$site" execute automotive_seed.runner.run --kwargs "$kwargs"
echo "$(date -u +%FT%TZ) SEED DONE site=$site profile=$profile"
