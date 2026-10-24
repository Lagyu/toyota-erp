#!/usr/bin/env bash
# docker compose for a deployment environment, assembled from the pinned frappe_docker
# files and the Hinode overrides (infra/deploy/).
#   compose.sh ENV_FILE <docker compose arguments...>
#   compose.sh /etc/hinode/production.env up -d
#   compose.sh /etc/hinode/production.env config        # review the merged file
# COMPOSE_EXTRA_FILES in the environment file adds compose files of the site's own (absolute
# paths, space separated), merged after the Hinode ones.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=deploy

env_file=${1:?usage: compose.sh ENV_FILE compose-args...}
shift
load_env_file "$REPO_ROOT/infra/versions.env"
load_env_file "$env_file"

upstream=${UPSTREAM_DIR:-$HOME/.cache/toyota-erp/upstream}
fd=$upstream/frappe_docker
if [[ ! -d $fd/.git ]]; then
	git clone --quiet --branch "$FRAPPE_DOCKER_REF" --depth 1 "$FRAPPE_DOCKER_REPO" "$fd"
fi
"$REPO_ROOT/scripts/lib/verify-checkout.sh" "$fd" "$FRAPPE_DOCKER_COMMIT" >/dev/null

deploy=$REPO_ROOT/infra/deploy
files=(
	-f "$fd/compose.yaml"
	-f "$fd/overrides/compose.mariadb.yaml"
	-f "$fd/overrides/compose.mariadb-secrets.yaml"
	-f "$fd/overrides/compose.redis.yaml"
)
case ${TLS_MODE:-files} in
	letsencrypt) files+=(-f "$deploy/compose.tls-letsencrypt.yaml") ;;
	files) files+=(-f "$deploy/compose.tls-files.yaml") ;;
	none) files+=(-f "$fd/overrides/compose.noproxy.yaml") ;;
	*) die "TLS_MODE must be letsencrypt, files or none" ;;
esac
files+=(-f "$deploy/compose.hinode.yaml")
[[ ${CLAMAV:-1} == 1 ]] && files+=(-f "$deploy/compose.clamav.yaml")
if [[ ${MONITORING:-0} == 1 ]]; then
	# Prometheus has no environment expansion: render the site name into its config
	sed "s/\${SITE_NAME}/$SITE_NAME/g" "$deploy/monitoring/prometheus.yml.tmpl" >"$deploy/monitoring/prometheus.rendered.yml"
	files+=(-f "$deploy/compose.monitoring.yaml")
	if [[ ${TLS_MODE:-files} != none ]]; then
		# the TLS proxy's own metrics: request rates and certificate expiry
		printf '\n  - job_name: traefik\n    static_configs: [{ targets: ["proxy:8082"] }]\n' \
			>>"$deploy/monitoring/prometheus.rendered.yml"
		files+=(-f "$deploy/compose.monitoring-proxy.yaml")
	fi
fi

# site-specific overrides (absolute paths, space separated), merged last
for extra in ${COMPOSE_EXTRA_FILES:-}; do
	files+=(-f "$extra")
done

# frappe_docker's compose.yaml pins nothing; the override supplies every image.
export ERPNEXT_VERSION=$ERPNEXT_REF
# Interpolation values go to compose as an owner-only env file: the environment itself
# does not survive sudo on hosts where the operator is not in the docker group.
compose_env=$(mktemp "${XDG_RUNTIME_DIR:-/tmp}/hinode-compose.XXXXXX")
trap 'rm -f "$compose_env"' EXIT
chmod 600 "$compose_env"
{
	grep -hE '^[A-Z_][A-Z0-9_]*=' "$REPO_ROOT/infra/versions.env" "$env_file"
	echo "ERPNEXT_VERSION=$ERPNEXT_VERSION"
} >"$compose_env"
cd "$deploy"
if docker info >/dev/null 2>&1; then runner=(docker); else runner=(sudo docker); fi
"${runner[@]}" compose --project-name "${COMPOSE_PROJECT_NAME:?}" --project-directory "$deploy" \
	--env-file "$compose_env" "${files[@]}" "$@"
