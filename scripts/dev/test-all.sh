#!/usr/bin/env bash
# Run every app's integration tests on the test site; summary per app; non-zero on failure.
set -uo pipefail
source "$(dirname "$0")/../lib/common.sh"
apps=(${@:-automotive_core automotive_manufacturing automotive_supply_chain automotive_portals automotive_seed})
status=0
for app in "${apps[@]}"; do
	log=/home/frappe/dev/logs/test-$app.log
	"$REPO_ROOT/scripts/dev/compose.sh" exec -T bench bash -lc \
		"cd ~/dev/frappe-bench && timeout 3000 bench --site \${DEV_TEST_SITE:-test.localhost} run-tests --app $app > $log 2>&1"
	rc=$?
	summary=$(grep -E "^(Ran|OK|FAILED)" "$REPO_ROOT/.local/dev-state/logs/test-$app.log" | tr '\n' ' ')
	printf '%-28s rc=%s %s\n' "$app" "$rc" "$summary"
	(( rc )) && status=1
done
exit $status
