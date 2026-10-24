#!/usr/bin/env bash
# Build the immutable application image from the pins in infra/versions.env.
#   build.sh [--reference] [--tag NAME] [--push REGISTRY/REPO]
# --reference  include automotive_seed (reference/staging sites only; never production)
# Tags: <name>:<version>-<gitsha>[-reference] and <name>:<version>[-reference].
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=image
load_env_file "$REPO_ROOT/infra/versions.env"

include_seed=0 name=toyota-erp/erp push=""
while (($#)); do
	case $1 in
		--reference) include_seed=1 ;;
		--tag) name=$2; shift ;;
		--push) push=$2; shift ;;
		*) die "unknown option $1" ;;
	esac
	shift
done

upstream=${UPSTREAM_DIR:-$HOME/.cache/toyota-erp/upstream}
fd=$upstream/frappe_docker
if [[ ! -d $fd/.git ]]; then
	git clone --quiet --branch "$FRAPPE_DOCKER_REF" --depth 1 "$FRAPPE_DOCKER_REPO" "$fd"
fi
"$REPO_ROOT/scripts/lib/verify-checkout.sh" "$fd" "$FRAPPE_DOCKER_COMMIT" >&2

version=$(python3 -c 'import re,sys; print(re.search(r"__version__\s*=\s*\"([^\"]+)\"", open(sys.argv[1]).read()).group(1))' \
	"$REPO_ROOT/apps/automotive_core/automotive_core/__init__.py")
sha=$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)
if [[ -n $(git -C "$REPO_ROOT" status --porcelain -- apps infra/image) ]]; then
	sha="$sha-dirty"
	log "working tree has uncommitted changes under apps/ or infra/image/; tagging $sha"
fi
suffix=$([[ $include_seed == 1 ]] && echo "-reference" || true)
tags=(-t "$name:$version-$sha$suffix" -t "$name:$version$suffix")

log "building $name:$version-$sha$suffix (frappe $FRAPPE_REF, erpnext $ERPNEXT_REF)"
docker_cmd buildx build \
	--file "$REPO_ROOT/infra/image/Containerfile" \
	--build-context "frappe_docker=$fd" \
	--build-arg "BUILD_IMAGE=$BUILD_IMAGE" \
	--build-arg "BASE_IMAGE=$BASE_IMAGE" \
	--build-arg "FRAPPE_REPO=$FRAPPE_REPO" --build-arg "FRAPPE_REF=$FRAPPE_REF" \
	--build-arg "FRAPPE_COMMIT=$FRAPPE_COMMIT" \
	--build-arg "ERPNEXT_REPO=$ERPNEXT_REPO" --build-arg "ERPNEXT_REF=$ERPNEXT_REF" \
	--build-arg "ERPNEXT_COMMIT=$ERPNEXT_COMMIT" \
	--build-arg "INCLUDE_SEED=$include_seed" \
	--build-arg "VERSION=$version" --build-arg "GIT_SHA=$sha" \
	--provenance=false \
	"${tags[@]}" \
	--load \
	"$REPO_ROOT"

if [[ -n $push ]]; then
	for tag in "$version-$sha$suffix" "$version$suffix"; do
		docker_cmd tag "$name:$tag" "$push:$tag"
		docker_cmd push "$push:$tag"
	done
fi
echo "$name:$version-$sha$suffix"
