#!/usr/bin/env bash
# Integration tests on a real Frappe/ERPNext bench at the pinned versions: brings up the
# development stack (MariaDB, Redis, bench, worker), bootstraps the bench and the test
# site, then runs every app's suite. Logs: .local/dev-state/logs/test-<app>.log
set -euo pipefail
cd "$(dirname "$0")/../.."
if [[ ${GITHUB_ACTIONS:-} == true ]]; then
	# the bench containers run as frappe (uid 1000) and write into the bind-mounted checkout
	# and dev state; a hosted runner checks out as its own user (an ephemeral machine)
	mkdir -p .local/dev-state/logs .local/dev-state/cache
	chmod -R a+rwX .
fi
./scripts/dev/up.sh
# up.sh runs the bench bootstrap detached: wait for its result before testing
./scripts/lib/wait-for-log.sh .local/dev-state/logs/bootstrap.log 'BOOTSTRAP OK' '\[bootstrap\] ERROR' 3600
./scripts/dev/test-all.sh "$@"
