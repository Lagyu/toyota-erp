#!/usr/bin/env bash
# Freeze the development bench's resolved Python dependencies into the image constraints
# (run after bootstrapping a bench at new upstream pins; review the diff before committing).
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
out="$REPO_ROOT/infra/image/constraints.txt"
{
	echo "# Python dependency lock for the application image (pip constraints), frozen from the"
	echo "# development bench at the versions in infra/versions.env. Regenerate with"
	echo "# scripts/image/freeze-constraints.sh after an upstream upgrade (ops/runbooks/upgrade.md)."
	"$REPO_ROOT/scripts/dev/compose.sh" exec -T bench bash -lc \
		'cd ~/dev/frappe-bench && env/bin/pip freeze --exclude-editable' | LC_ALL=C sort -f
} > "$out.tmp"
mv "$out.tmp" "$out"
echo "wrote $out ($(grep -vc '^#' "$out") packages)"
