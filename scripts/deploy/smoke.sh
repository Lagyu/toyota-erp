#!/usr/bin/env bash
# Post-deploy smoke test through the front end: liveness, readiness (database, cache,
# queue, scheduler), the realtime server, an authenticated API call, and the partner portal
# pages.
#   smoke.sh ENV_FILE
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=smoke
env_file=${1:?usage: smoke.sh ENV_FILE}
load_env_file "$env_file"
compose=("$REPO_ROOT/scripts/deploy/compose.sh" "$env_file")
site=${SITE_NAME:?}
fail=0
check() {  # expect: one code or alternatives separated by |
	local name=$1 url=$2 expect=$3
	local code
	code=$("${compose[@]}" exec -T frontend curl -s -o /dev/null -w '%{http_code}' -H "Host: $site" "http://127.0.0.1:8080$url")
	if [[ "|$expect|" == *"|$code|"* ]]; then log "ok   $name ($code)"; else log "FAIL $name: got $code, want $expect"; fail=1; fi
}
check live /api/method/automotive_core.ops.health.live 200
check ready /api/method/automotive_core.ops.health.ready 200
check metrics-needs-token /api/method/automotive_core.ops.health.metrics 403
check login-page /login 200
check supplier-portal-guest /supplier-portal 302
check dealer-portal-guest /dealer-portal 302
check desk-guest /desk "301|302"
# the realtime server (socket.io behind the front end): the transport handshake needs no session
check realtime-handshake "/socket.io/?EIO=4&transport=polling" 200
admin=$(<"${ADMIN_PASSWORD_FILE:?}")
login=$("${compose[@]}" exec -T -e PW="$admin" frontend sh -c \
	"curl -s -o /dev/null -w '%{http_code}' -H 'Host: $site' -H 'Content-Type: application/json' \
	-d \"{\\\"usr\\\": \\\"Administrator\\\", \\\"pwd\\\": \\\"\$PW\\\"}\" http://127.0.0.1:8080/api/method/login")
[[ $login == 200 ]] && log "ok   administrator login" || { log "FAIL administrator login ($login)"; fail=1; }
exit $fail
