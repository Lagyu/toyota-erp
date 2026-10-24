# Shared helpers for repository scripts. Source, don't execute.
# shellcheck shell=bash

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
export REPO_ROOT

log() { printf '%s [%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${LOG_TAG:-script}" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# Load KEY=VALUE pairs (no shell expansion) from an env file into the environment.
load_env_file() {
	local file=$1 line key value
	[[ -f $file ]] || die "missing env file: $file"
	while IFS= read -r line || [[ -n $line ]]; do
		[[ $line =~ ^[[:space:]]*(#|$) ]] && continue
		key=${line%%=*}
		value=${line#*=}
		[[ $key =~ ^[A-Z_][A-Z0-9_]*$ ]] || die "bad key in $file: $key"
		export "$key=$value"
	done <"$file"
}

# Docker needs root on hosts where the operator is not in the docker group.
docker_cmd() {
	if docker info >/dev/null 2>&1; then docker "$@"; else sudo docker "$@"; fi
}

random_secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }

# A secret file the application containers read as frappe (uid 1000), as preflight.sh
# requires: owned by uid 1000 and group-readable by the deploying user. A no-op when that
# user is uid 1000; otherwise (a CI runner) it needs sudo.
secret_for_frappe() {
	local file
	for file in "$@"; do
		[[ $(id -u) == 1000 ]] && continue
		chmod 0440 "$file"
		sudo chown "1000:$(id -g)" "$file"
	done
}

# Python for a frappe container (working directory: the bench) that sets the site's
# Administrator password to the value on its stdin (never on a command line).
#   compose exec -T backend env/bin/python -c "$SET_ADMIN_PASSWORD_PY" SITE <secret_file
# shellcheck disable=SC2034
SET_ADMIN_PASSWORD_PY='import os, sys
import frappe
from frappe.utils.password import update_password
os.chdir("sites")  # Frappe opens its log files at ../logs from the sites directory
frappe.init(site=sys.argv[1], sites_path=".")
frappe.connect()
update_password("Administrator", sys.stdin.read().strip())
frappe.db.commit()
frappe.destroy()'

# Python for a frappe container (working directory: the bench) that sets one
# site_config.json key to the value on its stdin. Unlike bench set-config, the value never
# appears on a command line, and one that starts with "-" (token_urlsafe gives those) is
# not taken for an option.
#   compose... exec -T backend python3 -c "$SET_SITE_CONFIG_PY" SITE KEY <<<"$value"
# shellcheck disable=SC2034
SET_SITE_CONFIG_PY='import json, os, sys
site, key = sys.argv[1:3]
path = os.path.join("sites", site, "site_config.json")
with open(path) as handle:
	config = json.load(handle)
config[key] = sys.stdin.read().rstrip("\n")
tmp = path + ".tmp"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
	json.dump(config, handle, indent=1, sort_keys=True)
os.replace(tmp, path)'
