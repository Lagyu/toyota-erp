#!/usr/bin/env bash
# Pinned CI tool versions, installed into .local/tools/venv (shared by local runs and CI).
#   source scripts/ci/tools.sh   ->  $CI_BIN on PATH
# shellcheck shell=bash
set -euo pipefail
CI_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CI_VENV=${CI_VENV:-$CI_ROOT/.local/tools/venv}
CI_BIN=$CI_VENV/bin
TOOL_PINS=(ruff==0.14.1 shellcheck-py==0.10.0.1 pip-audit==2.9.0 PyYAML==6.0.3)
if [[ ! -x $CI_BIN/python ]]; then
	python3 -m venv "$CI_VENV"
fi
if ! "$CI_BIN/python" -m pip freeze 2>/dev/null | grep -qx "pip-audit==2.9.0"; then
	"$CI_BIN/python" -m pip install --quiet --disable-pip-version-check "${TOOL_PINS[@]}"
fi
export PATH=$CI_BIN:$PATH
