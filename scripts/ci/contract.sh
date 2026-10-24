#!/usr/bin/env bash
# API contract tests (integration_tests/): the running API against the committed OpenAPI
# document.
#   contract.sh --url http://small.localhost:18000 --site small.localhost [--password-file FILE]
# Signs in as Administrator. The password comes from --password-file (a deployed stack's
# admin password secret), else the development stack's DEV_ADMIN_PASSWORD (.local/dev.env);
# it reaches the tests through the environment, never the command line.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=contract
url="" site="" password_file=""
while (($#)); do
	case $1 in
		--url) url=$2; shift 2 ;;
		--site) site=$2; shift 2 ;;
		--password-file) password_file=$2; shift 2 ;;
		*) die "usage: contract.sh --url URL [--site SITE] [--password-file FILE]" ;;
	esac
done
[[ -n $url ]] || die "--url is required"
if [[ -n $password_file ]]; then
	INTEGRATION_PASSWORD=$(<"$password_file")
else
	load_env_file "$REPO_ROOT/.local/dev.env"
	INTEGRATION_PASSWORD=${DEV_ADMIN_PASSWORD:?no --password-file and no DEV_ADMIN_PASSWORD in .local/dev.env}
fi
export INTEGRATION_URL=$url INTEGRATION_HOST=${site:-} INTEGRATION_PASSWORD
cd "$REPO_ROOT"
log "contract tests against $url${site:+ ($site)}"
python3 -m unittest -v integration_tests.test_api_contract
