#!/usr/bin/env bash
# The deploy compose as Docker Compose resolves it (`config`: nothing is started), for each
# TLS mode with monitoring and ClamAV on: every service restarts with the Docker daemon
# except frappe_docker's one-shot configurator (whose dependants wait for it to complete),
# and no container mounts the Docker socket (ops/security/findings.md C-02). Then an
# application host of the high-availability option (DATASTORES=external, SINGLETONS=0): no
# MariaDB or Redis of its own, the external ones in the configurator's settings, and no
# scheduler or cron running.
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

# an application host of the high-availability option
sed -e "s/^TLS_MODE=.*/TLS_MODE=files/" "$REPO_ROOT/config/env/production.env.example" >"$work/ha.env"
printf 'DATASTORES=external\nDB_HOST=db.ha.example\nREDIS_CACHE=cache.ha.example:6379\nREDIS_QUEUE=queue.ha.example:6379\nSINGLETONS=0\n' \
	>>"$work/ha.env"
"$REPO_ROOT/scripts/deploy/compose.sh" "$work/ha.env" config --format json >"$work/ha.json"
python3 - "$work/ha.json" <<'EOF' || status=1
import json
import sys

config = json.load(open(sys.argv[1]))
services = config["services"]
problems = [f"{name}: still in the stack" for name in ("db", "redis-cache", "redis-queue") if name in services]
env = services["configurator"].get("environment") or {}
wanted = {"DB_HOST": "db.ha.example", "DB_PORT": "3306", "REDIS_CACHE": "cache.ha.example:6379",
	"REDIS_QUEUE": "queue.ha.example:6379"}
problems += [f"configurator {k}={env.get(k)!r}, not {v!r}" for k, v in wanted.items() if str(env.get(k)) != v]
for name in ("scheduler", "cron"):
	replicas = (services[name].get("deploy") or {}).get("replicas")
	if replicas != 0:
		problems.append(f"{name}: {replicas} replicas on a host without the singletons")
# every service that uses MariaDB or Redis can reach them: the backend network has no route out
for name in ("backend", "queue-short", "queue-long", "websocket"):
	if "egress" not in (services[name].get("networks") or {}):
		problems.append(f"{name}: not on the outbound network, cannot reach external datastores")
print(f"DATASTORES=external SINGLETONS=0: {len(services)} services, "
	+ ("ok" if not problems else "\n  " + "\n  ".join(problems)))
sys.exit(1 if problems else 0)
EOF
exit $status
