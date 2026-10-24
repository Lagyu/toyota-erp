#!/usr/bin/env bash
# (Re)start the development web server: every dev site on one port, chosen by the Host
# header (http://<site>:${DEV_HTTP_PORT:-18000}, e.g. small.localhost for the end-to-end suite).
# Log: .local/dev-state/logs/serve.log
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=dev-serve
# the bracket keeps the pattern from matching this command itself
"$REPO_ROOT/scripts/dev/compose.sh" exec -T bench bash -c 'pkill -f "[s]erve --port 8000" || true'
"$REPO_ROOT/scripts/dev/compose.sh" exec -d bench bash -lc \
	'cd /home/frappe/dev/frappe-bench && exec bench serve --port 8000 --noreload >/home/frappe/dev/logs/serve.log 2>&1'
log "dev web server (re)started on port ${DEV_HTTP_PORT:-18000} (log: .local/dev-state/logs/serve.log)"
