#!/usr/bin/env bash
# Remove DEFINER clauses from a (gzipped) MariaDB dump so it can be restored by the site's
# own database user. Frappe dumps include the automotive immutability triggers; with a
# DEFINER naming another account (or the same account under a different host scope) the
# restore would need SET USER / SUPER. Without it the triggers belong to the restoring
# user, and `bench migrate` re-applies the guards anyway.
#   strip-definers.sh IN.sql.gz OUT.sql.gz
set -euo pipefail
in=${1:?input dump} out=${2:?output dump}
tmp="$out.tmp"
zcat -f "$in" | sed -E 's#/\*!50017 DEFINER=`[^`]*`@`[^`]*`\*/ ?##g; s#DEFINER=`[^`]*`@`[^`]*` ##g' | gzip -c > "$tmp"
mv "$tmp" "$out"
