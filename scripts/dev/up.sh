#!/usr/bin/env bash
# Start the development stack and run the (idempotent) bench bootstrap detached.
# Progress: .local/dev-state/logs/bootstrap.log (ends with "BOOTSTRAP OK").
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
LOG_TAG=dev-up
"$REPO_ROOT/scripts/dev/init-env.sh" >/dev/null
"$REPO_ROOT/scripts/dev/compose.sh" up -d --wait
log "running bootstrap in the bench container (log: .local/dev-state/logs/bootstrap.log)"
"$REPO_ROOT/scripts/dev/compose.sh" exec -d bench bash -lc \
	'/workspace/scripts/dev/bootstrap-bench.sh >>/home/frappe/dev/logs/bootstrap.log 2>&1'
