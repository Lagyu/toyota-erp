#!/usr/bin/env bash
# The deploy compose as Docker Compose resolves it (`config`: nothing is started), for each
# TLS mode with monitoring and ClamAV on: every service restarts with the Docker daemon
# except frappe_docker's one-shot configurator (whose dependants wait for it to complete),
# and no container mounts the Docker socket (ops/security/findings.md C-02).
#   compose-check.sh   (CI lint job; needs the docker CLI with the compose plugin)
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
status=0
for tls in files letsencrypt none; do
	sed -e "s/^TLS_MODE=.*/TLS_MODE=$tls/" "$REPO_ROOT/config/env/production.env.example" >"$work/$tls.env"
	echo "CLAMAV=1" >>"$work/$tls.env"
	"$REPO_ROOT/scripts/deploy/compose.sh" "$work/$tls.env" config --format json >"$work/$tls.json"
	python3 - "$work/$tls.json" "$tls" <<'EOF' || status=1
import json
import sys

config = json.load(open(sys.argv[1]))
problems = []
for name, service in sorted(config["services"].items()):
	restart = service.get("restart", "no")
	if name == "configurator":
		if restart not in ("on-failure", "no"):
			problems.append(f"{name}: restart {restart} (a one-shot job: its dependants wait for it to complete)")
	elif restart not in ("always", "unless-stopped"):
		problems.append(f"{name}: restart {restart} (would not come back after a reboot)")
	for volume in service.get("volumes") or []:
		if "docker.sock" in str(volume.get("source", "")):
			problems.append(f"{name}: mounts the Docker socket")
print(f"TLS_MODE={sys.argv[2]}: {len(config['services'])} services, "
	+ ("ok" if not problems else "\n  " + "\n  ".join(problems)))
sys.exit(1 if problems else 0)
EOF
done
exit $status
