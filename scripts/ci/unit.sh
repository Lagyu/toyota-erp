#!/usr/bin/env bash
# Pure-Python unit tests (no bench, no database): the deterministic planning, sequencing,
# option-rule and VIN engines, and the backup tooling's binary log logic. Frappe-dependent
# tests run in integration.sh.
set -euo pipefail
source "$(dirname "$0")/tools.sh"
cd "$CI_ROOT/apps/automotive_manufacturing"
python3 -s -m unittest automotive_manufacturing.tests.test_engine -v 2>&1 | tail -20
# the operations scripts' pure logic (binary log positions, replay order, mirror retention)
cd "$CI_ROOT/ops/backup"
python3 -s -m unittest test_binlog -v 2>&1 | tail -12
