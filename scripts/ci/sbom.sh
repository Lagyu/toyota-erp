#!/usr/bin/env bash
# Software bill of materials and vulnerability scan of an application image.
#   sbom.sh IMAGE   ->  .local/sbom/<tag>.spdx.json, .local/sbom/<tag>.trivy.json,
#                       .local/sbom/<tag>.licences.md (scripts/ci/licences.py)
# Fails on fixable CRITICAL vulnerabilities (HIGH are reported) unless
# config/security/image-allowlist.txt accepts them with a finding of
# ops/security/findings.md. SBOM_RELEASE=1 (release builds): every accepted finding also
# needs its owner decision recorded there (GATE-5).
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
image=${1:?image}
load_env_file "$REPO_ROOT/infra/ci-tools.env"
out=$REPO_ROOT/.local/sbom
mkdir -p "$out"
tag=$(tr '/:@' '___' <<<"$image")
docker_cmd run --rm -v /var/run/docker.sock:/var/run/docker.sock "$SYFT_IMAGE" \
	"docker:$image" -o spdx-json >"$out/$tag.spdx.json"
echo "SBOM: $out/$tag.spdx.json ($(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["packages"]))' "$out/$tag.spdx.json") packages)"
# the licence inventory beside it (ops/security/licences.md is the committed copy of a baseline)
python3 "$REPO_ROOT/scripts/ci/licences.py" "$out/$tag.spdx.json" >"$out/$tag.licences.md"
echo "licences: $out/$tag.licences.md ($(grep -m1 -A0 '^## Unknown' "$out/$tag.licences.md" | tr -d '#'))"
docker_cmd run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$out:/out" "$TRIVY_IMAGE" image \
	--quiet --scanners vuln --format json --output "/out/$tag.trivy.json" "$image"
python3 - "$out/$tag.trivy.json" "$REPO_ROOT/config/security/image-allowlist.txt" \
	"$REPO_ROOT/ops/security/findings.md" "${SBOM_RELEASE:-0}" <<'PY'
import json, sys
report_path, allowlist_path, findings_path, release = sys.argv[1:5]
allow = {}
for line in open(allowlist_path):
    fields = line.split("#", 1)[0].split()
    if fields:
        package, version, finding = fields[:3]
        allow[(package.lower(), version)] = finding
report = json.load(open(report_path))
counts, fixable_critical, accepted = {}, [], {}
for result in report.get("Results", []):
    for v in result.get("Vulnerabilities") or []:
        counts[v["Severity"]] = counts.get(v["Severity"], 0) + 1
        if v["Severity"] == "CRITICAL" and v.get("FixedVersion"):
            finding = allow.get((v["PkgName"].lower(), v.get("InstalledVersion")))
            if finding:
                accepted.setdefault(finding, set()).add(f'{v["PkgName"]} {v.get("InstalledVersion")}: {v["VulnerabilityID"]}')
            else:
                fixable_critical.append(f'{v["VulnerabilityID"]} {v["PkgName"]} {v.get("InstalledVersion")} -> {v["FixedVersion"]}')
print("vulnerabilities:", counts or "none")
for finding, items in sorted(accepted.items()):
    print(f"accepted as {finding}: " + ", ".join(sorted(items)))
failed = False
if fixable_critical:
    print("fixable CRITICAL:\n  " + "\n  ".join(sorted(set(fixable_critical))))
    failed = True
if release == "1":
    # the findings table's last column: the owner's decision (name, date)
    decided = {}
    for row in open(findings_path):
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) >= 6 and cells[0] in accepted:
            decided[cells[0]] = cells[-1]
    for finding in sorted(accepted):
        if not decided.get(finding):
            print(f"release: {finding} has no owner decision in ops/security/findings.md")
            failed = True
sys.exit(1 if failed else 0)
PY
