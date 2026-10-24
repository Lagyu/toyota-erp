#!/usr/bin/env bash
# scripts/dev/i18n.py without the containers: the host's Python 3.14 with the bootstrapped
# development bench's sources and packages (.local/dev-state/frappe-bench, read only), for
# when the development stack is stopped. Writes the catalogues in this repository.
#   i18n-host.sh pot | po ja | stats ja | check ja
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
bench=$REPO_ROOT/.local/dev-state/frappe-bench
[[ -d $bench/apps/frappe && -d $bench/env ]] || die "no bootstrapped bench in $bench (scripts/dev/up.sh creates it)"
cd "$bench/sites"
export I18N_APPS_DIR=$REPO_ROOT/apps
exec python3 -c '
import runpy, site, sys
bench, repo = sys.argv[1], sys.argv[2]
# the bench packages (and their .pth entries) ahead of the system ones
before = list(sys.path)
site.addsitedir(f"{bench}/env/lib/python3.14/site-packages")
sys.path[:] = [p for p in sys.path if p not in before] + before
apps = ["automotive_core", "automotive_manufacturing", "automotive_supply_chain", "automotive_portals", "automotive_seed"]
# the bench links the custom apps from the container path /workspace: import them from here
sys.path[:0] = [f"{bench}/apps/frappe", f"{bench}/apps/erpnext"] + [f"{repo}/apps/{app}" for app in apps]
# extractors locate files relative to the bench: this repository has the same apps/ layout
import frappe.utils
frappe.utils.get_bench_path = lambda: repo
sys.argv = [f"{repo}/scripts/dev/i18n.py", *sys.argv[3:]]
runpy.run_path(sys.argv[0], run_name="__main__")
' "$bench" "$REPO_ROOT" "$@"
