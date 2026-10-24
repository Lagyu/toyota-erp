#!/usr/bin/env bash
# Integration tests on a real Frappe/ERPNext bench at the pinned versions: brings up the
# development stack (MariaDB, Redis, bench, worker), bootstraps the bench and the test
# site, then runs every app's suite and the translation check.
# Logs: .local/dev-state/logs/test-<app>.log
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
status=0
./scripts/dev/test-all.sh "$@" || status=1
# every translatable string in the code has a Japanese translation (lint.sh checks only the
# strings already in the catalogues)
./scripts/dev/compose.sh exec -T bench bash -lc \
	'cd ~/dev/frappe-bench/sites && ../env/bin/python /workspace/scripts/dev/i18n.py check ja' || status=1
exit $status
