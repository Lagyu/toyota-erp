#!/usr/bin/env bash
# docker compose for the development stack with the pinned versions and local secrets.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
[[ -f $REPO_ROOT/.local/dev.env ]] || "$REPO_ROOT/scripts/dev/init-env.sh" >/dev/null
docker_cmd compose \
	--project-directory "$REPO_ROOT/infra/dev" \
	--env-file "$REPO_ROOT/infra/versions.env" \
	--env-file "$REPO_ROOT/.local/dev.env" \
	-f "$REPO_ROOT/infra/dev/compose.yaml" "$@"
