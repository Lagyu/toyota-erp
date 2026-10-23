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
