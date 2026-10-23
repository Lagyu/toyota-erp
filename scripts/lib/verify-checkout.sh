#!/usr/bin/env bash
# verify-checkout.sh DIR EXPECTED_COMMIT
# Fails unless the git checkout in DIR is exactly EXPECTED_COMMIT. Used after every
# clone of a pinned upstream ref so that a moved tag cannot change what we build.
set -euo pipefail
dir=$1 expected=$2
actual=$(git -C "$dir" rev-parse HEAD)
if [[ $actual != "$expected" ]]; then
	echo "pin mismatch in $dir: expected $expected, found $actual" >&2
	exit 1
fi
echo "verified $dir @ $actual"
