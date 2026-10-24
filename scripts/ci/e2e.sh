#!/usr/bin/env bash
# End-to-end suite (Playwright, pinned image) against a reference site with the synthetic
# dataset. Two modes:
#   e2e.sh --url http://small.localhost:18000 --site small.localhost [-- playwright args]
# Persona passwords (automotive_seed.e2e.prepare_users) and journey preconditions
# (automotive_seed.e2e.prepare_journeys) are generated for the run and kept in
# .local/e2e/{credentials,fixtures}.json (0600); reports in .local/e2e/.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=e2e
load_env_file "$REPO_ROOT/infra/ci-tools.env"
url="" site="" pw_args=()
while (($#)); do
	case $1 in
		--url) url=$2; shift 2 ;;
		--site) site=$2; shift 2 ;;
		--) shift; pw_args=("$@"); break ;;
		*) die "unknown argument $1 (usage: e2e.sh --url URL --site SITE [-- playwright args])" ;;
	esac
done
[[ -n $url && -n $site ]] || die "usage: e2e.sh --url URL --site SITE"
out=$REPO_ROOT/.local/e2e
mkdir -p "$out"
umask 077
# personas (one-run passwords) and the journeys' preconditions (staged material, partner
# signing secret): both files are 0600 and mounted read-only into the Playwright container
for step in "prepare_users credentials" "prepare_journeys fixtures"; do
	read -r fn file <<<"$step"
	"$REPO_ROOT/scripts/dev/compose.sh" exec -T bench bash -lc \
		"cd ~/dev/frappe-bench && bench --site '$site' execute automotive_seed.e2e.$fn \
		--kwargs \"{'output': '/workspace/.local/e2e/$file.json'}\"" | tail -1
done
log "running Playwright against $url ($site)"
docker_cmd run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp -e CI="${CI:-}" \
	-e E2E_BASE_URL="$url" -e E2E_CREDENTIALS=/creds/credentials.json -e E2E_FIXTURES=/creds/fixtures.json \
	-e E2E_REPORT_DIR=/report \
	-v "$REPO_ROOT/e2e:/e2e" -v "$out:/creds:ro" -v "$out:/report" -w /e2e "$PLAYWRIGHT_IMAGE" \
	bash -lc 'test -d node_modules/@playwright/test || npm ci --no-audit --no-fund >/dev/null; npx playwright test "$@"' \
	e2e "${pw_args[@]}"
