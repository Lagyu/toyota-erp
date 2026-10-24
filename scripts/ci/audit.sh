#!/usr/bin/env bash
# Dependency audit: known vulnerabilities in the locked Python dependencies of the
# application image (OSV / PyPI advisory database). Findings must be fixed or recorded
# with a justification in config/security/audit-allowlist.txt (one "ID  reason" per line).
set -euo pipefail
source "$(dirname "$0")/tools.sh"
cd "$CI_ROOT"
ignores=()
if [[ -f config/security/audit-allowlist.txt ]]; then
	while read -r id _reason; do
		[[ -z $id || $id == \#* ]] && continue
		ignores+=(--ignore-vuln "$id")
	done <config/security/audit-allowlist.txt
fi
# URL/VCS pins (frappe's gunicorn fork) cannot be looked up by version: listed, not audited
grep -v '^#' infra/image/constraints.txt | grep -v ' @ ' >"$CI_ROOT/.local/constraints-audit.txt"
if grep -q ' @ ' infra/image/constraints.txt; then
	echo "not audited (URL pins): $(grep ' @ ' infra/image/constraints.txt | cut -d' ' -f1 | tr '\n' ' ')"
fi
pip-audit --strict --progress-spinner off --requirement "$CI_ROOT/.local/constraints-audit.txt" --no-deps \
	--disable-pip "${ignores[@]}"
