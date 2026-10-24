#!/usr/bin/env bash
# Pure-Python unit tests (no bench, no database): the deterministic planning, sequencing,
# option-rule and VIN engines. Frappe-dependent tests run in integration.sh.
set -euo pipefail
source "$(dirname "$0")/tools.sh"
cd "$CI_ROOT/apps/automotive_manufacturing"
python3 -s -m unittest automotive_manufacturing.tests.test_engine -v 2>&1 | tail -20
