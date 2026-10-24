#!/usr/bin/env bash
# Software bill of materials and vulnerability scan of an application image.
#   sbom.sh IMAGE   ->  .local/sbom/<tag>.spdx.json, .local/sbom/<tag>.trivy.json
# Fails on fixable CRITICAL vulnerabilities (HIGH are reported).
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
docker_cmd run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$out:/out" "$TRIVY_IMAGE" image \
	--quiet --scanners vuln --format json --output "/out/$tag.trivy.json" "$image"
python3 - "$out/$tag.trivy.json" <<'PY'
import json, sys
report = json.load(open(sys.argv[1]))
counts, fixable_critical = {}, []
for result in report.get("Results", []):
    for v in result.get("Vulnerabilities") or []:
        counts[v["Severity"]] = counts.get(v["Severity"], 0) + 1
        if v["Severity"] == "CRITICAL" and v.get("FixedVersion"):
            fixable_critical.append(f'{v["VulnerabilityID"]} {v["PkgName"]} {v.get("InstalledVersion")} -> {v["FixedVersion"]}')
print("vulnerabilities:", counts or "none")
if fixable_critical:
    print("fixable CRITICAL:\n  " + "\n  ".join(fixable_critical))
    sys.exit(1)
PY
