#!/usr/bin/env bash
# Static checks: ruff, shellcheck, generated-schema drift, YAML/JSON syntax, and the
# identity boundary (no third-party trademarks in customer-facing assets).
set -euo pipefail
source "$(dirname "$0")/tools.sh"
cd "$CI_ROOT"
status=0
step() { printf '\n== %s\n' "$*"; }

step "ruff"
ruff check apps scripts integration_tests perf ops || status=1

step "shellcheck"
find scripts ops -name '*.sh' -print0 | xargs -0 shellcheck -x -S warning || status=1

step "GitHub workflows (actionlint)"
actionlint -shellcheck "$(command -v shellcheck)" .github/workflows/*.yml || status=1

step "schema and desk JSON are generated from the specs"
python3 scripts/dev/schema/generate.py --check || status=1

step "OpenAPI contract matches the whitelisted methods"
python3 scripts/dev/openapi.py --check || status=1

step "OpenAPI contract version follows its changes (D-034)"
# CONTRACT_BASE: the commit before the push, or a pull request's base (set by ci.yml)
contract=apps/automotive_core/automotive_core/integration_hub/openapi.json
base="${CONTRACT_BASE:-}"
if [[ -z $base || $base =~ ^0+$ ]]; then
	echo "no earlier commit to compare with"
elif ! git cat-file -e "$base^{commit}" 2>/dev/null && ! git fetch -q --depth=1 origin "$base"; then
	echo "earlier commit $base not reachable: not compared"
elif ! git cat-file -e "$base:$contract" 2>/dev/null; then
	echo "no contract at $base"
else
	git show "$base:$contract" > "${TMPDIR:-/tmp}/openapi-base.json"
	python3 scripts/dev/openapi.py --compat "${TMPDIR:-/tmp}/openapi-base.json" || status=1
fi

step "actions that write past the permissions check the caller's scope (C-10)"
python3 scripts/dev/check_scoped_actions.py || status=1

step "translations: complete, placeholders and HTML tags kept"
python3 scripts/dev/check_po.py --require-complete apps/*/*/locale/ja.po || status=1

step "YAML / JSON syntax"
python3 - <<'PY' || status=1
import json, pathlib, sys
import yaml
bad = []
for path in pathlib.Path(".").rglob("*"):
    if any(part in {".git", ".local", "node_modules", "dist"} for part in path.parts) or not path.is_file():
        continue
    try:
        if path.suffix in {".yaml", ".yml"}:
            list(yaml.safe_load_all(path.read_text()))
        elif path.suffix == ".json":
            json.loads(path.read_text())
    except Exception as exc:
        bad.append(f"{path}: {exc}")
print("\n".join(bad) or "ok")
sys.exit(1 if bad else 0)
PY

step "e2e suite: TypeScript type check"
if command -v npm >/dev/null; then
	(cd e2e && npm ci --ignore-scripts --no-audit --no-fund >/dev/null && npx --no-install tsc --noEmit -p .) || status=1
else
	echo "npm not found: skipped (CI runners have Node)"
fi

step "identity boundary (customer-facing assets)"
python3 scripts/ci/identity_check.py || status=1

exit $status
