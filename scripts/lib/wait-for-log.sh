#!/usr/bin/env bash
# wait-for-log.sh LOG SUCCESS_ERE FAILURE_ERE [TIMEOUT_SECONDS]
# Polls LOG until a line matches SUCCESS_ERE (exit 0) or FAILURE_ERE (exit 1).
set -uo pipefail
log=$1 ok=$2 bad=$3 timeout=${4:-3600}
start=$(date +%s)
while :; do
	if [[ -f $log ]]; then
		if grep -qE -- "$bad" "$log"; then echo "FAILED: $(grep -nE -- "$bad" "$log" | head -3)"; tail -25 "$log"; exit 1; fi
		if grep -qE -- "$ok" "$log"; then echo "OK: $(grep -E -- "$ok" "$log" | tail -1)"; exit 0; fi
	fi
	if (( $(date +%s) - start > timeout )); then echo "TIMEOUT after ${timeout}s"; tail -15 "$log" 2>/dev/null; exit 2; fi
	sleep 10
done
