#!/usr/bin/env bash
# Install Docker Engine, Compose v2 and Buildx from the Ubuntu archive and run
# the engine, containerd and every container inside one resource-capped systemd
# slice (erp.slice). An engine already installed from Docker's repository
# (docker-ce) or Microsoft's (moby-engine) is kept: the archive's packages conflict
# with it.
#
# The slice is what keeps an ERP stack from starving co-located services: the
# kernel enforces MemoryMax on everything Docker starts (containers, BuildKit
# steps, page cache they fault in), and the low CPU/IO weights make the stack
# yield under contention. On a dedicated ERP host set ERP_SLICE_MEMORY_MAX=infinity
# and DOCKER_START_AT_BOOT=yes.
#
# Idempotent: re-running only rewrites the managed files and restarts Docker if
# one of them changed.
set -euo pipefail

: "${ERP_SLICE_MEMORY_HIGH:=8G}"
: "${ERP_SLICE_MEMORY_MAX:=10G}"
: "${ERP_SLICE_SWAP_MAX:=1G}"
: "${ERP_SLICE_CPU_WEIGHT:=20}"
: "${ERP_SLICE_IO_WEIGHT:=20}"
: "${DOCKER_START_AT_BOOT:=no}"

if [[ $EUID -ne 0 ]]; then
	exec sudo --preserve-env=ERP_SLICE_MEMORY_HIGH,ERP_SLICE_MEMORY_MAX,ERP_SLICE_SWAP_MAX,ERP_SLICE_CPU_WEIGHT,ERP_SLICE_IO_WEIGHT,DOCKER_START_AT_BOOT "$0" "$@"
fi

changed=0
write_if_changed() { # path content
	local path=$1 content=$2
	if [[ ! -f $path ]] || [[ "$(cat "$path")" != "$content" ]]; then
		mkdir -p "$(dirname "$path")"
		printf '%s\n' "$content" >"$path"
		echo "wrote $path"
		changed=1
	fi
}

write_if_changed /etc/systemd/system/erp.slice "[Unit]
Description=Toyota ERP Clone container workloads (resource-capped)
Before=slices.target

[Slice]
MemoryAccounting=yes
CPUAccounting=yes
IOAccounting=yes
MemoryHigh=${ERP_SLICE_MEMORY_HIGH}
MemoryMax=${ERP_SLICE_MEMORY_MAX}
MemorySwapMax=${ERP_SLICE_SWAP_MAX}
CPUWeight=${ERP_SLICE_CPU_WEIGHT}
IOWeight=${ERP_SLICE_IO_WEIGHT}
TasksMax=16384"

for unit in docker.service containerd.service; do
	write_if_changed "/etc/systemd/system/${unit}.d/10-erp-slice.conf" "[Service]
Slice=erp.slice"
done

write_if_changed /etc/docker/daemon.json '{
  "cgroup-parent": "erp.slice",
  "log-driver": "local",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "features": { "buildkit": true }
}'

systemctl daemon-reload

installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "ok installed"; }
if installed docker-ce || installed moby-engine; then
	echo "keeping the installed Docker Engine ($(installed docker-ce && echo docker-ce || echo moby-engine))"
elif ! installed docker.io; then
	apt-get update -qq
	DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
		docker.io docker-compose-v2 docker-buildx
	changed=1
fi

if [[ $DOCKER_START_AT_BOOT == yes ]]; then
	systemctl enable containerd.service docker.service docker.socket
else
	systemctl disable containerd.service docker.service docker.socket 2>/dev/null || true
fi

if (( changed )); then
	systemctl restart containerd.service docker.service
else
	systemctl start docker.service
fi

docker version --format 'docker engine {{.Server.Version}}'
docker compose version
docker buildx version
systemctl show erp.slice -p MemoryHigh -p MemoryMax -p CPUWeight -p IOWeight
