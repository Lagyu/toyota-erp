#!/usr/bin/env bash
# Start the development stack and run the (idempotent) bench bootstrap detached.
# Progress: .local/dev-state/logs/bootstrap.log (ends with "BOOTSTRAP OK").
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=dev-up
"$REPO_ROOT/scripts/dev/init-env.sh" >/dev/null
"$REPO_ROOT/scripts/dev/compose.sh" up -d --wait
# a fresh log per run, so waiters do not see the previous run's result
logs=$REPO_ROOT/.local/dev-state/logs
[[ -f $logs/bootstrap.log ]] && mv "$logs/bootstrap.log" "$logs/bootstrap.log.1"
log "running bootstrap in the bench container (log: .local/dev-state/logs/bootstrap.log)"
"$REPO_ROOT/scripts/dev/compose.sh" exec -d bench bash -lc \
	'/workspace/scripts/dev/bootstrap-bench.sh >>/home/frappe/dev/logs/bootstrap.log 2>&1'
