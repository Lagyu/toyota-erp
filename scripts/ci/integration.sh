#!/usr/bin/env bash
# Integration tests on a real Frappe/ERPNext bench at the pinned versions: brings up the
# development stack (MariaDB, Redis, bench, worker), bootstraps the bench and the test
# site, then runs every app's suite. Logs: .local/dev-state/logs/test-<app>.log
set -euo pipefail
cd "$(dirname "$0")/../.."
./scripts/dev/up.sh
./scripts/dev/test-all.sh "$@"
