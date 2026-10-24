#!/usr/bin/env bash
# Reboot persistence on a real host (ops/runbooks/install-fresh-host.md step 11; OPS-6, GATE-6),
# which a hosted runner cannot test on itself: a disposable Ubuntu VM under KVM on this host,
# installed by the runbook with the production example's settings and IMAGE, rebooted, then
# reset without a shutdown (a power loss), with the runbook's checks after each restart.
#   reboot-check.sh IMAGE
# The VM boots the cloud image pinned in infra/ci-tools.env (its signed checksum checked) with
# 4 vCPU and 12 GB; the steps inside it are scripts/ci/reboot-guest.sh, on a copy of this
# commit's tree. Needs /dev/kvm, sudo, and IMAGE in this host's Docker.
# Report: .local/reboot/report.md, with the guest's logs and the serial console beside it.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
export LOG_TAG=reboot
load_env_file "$REPO_ROOT/infra/ci-tools.env"
image=${1:?usage: reboot-check.sh IMAGE}
out=$REPO_ROOT/.local/reboot
vm=$REPO_ROOT/.local/reboot-vm  # disks, SSH key, monitor socket: not evidence
rm -rf "$out" "$vm"
mkdir -p "$out/guest" "$vm"
report=$out/report.md
# shellcheck source=scripts/lib/host-install.sh
source "$(dirname "$0")/../lib/host-install.sh"
{
	echo "# Reboot persistence on a real host"
	echo
	echo "\`$image\` installed by ops/runbooks/install-fresh-host.md on a disposable VM, rebooted, then reset"
	echo "without a shutdown; $(date -u +%FT%TZ)."
	echo
	echo "| Step | Result | Seconds | Detail |"
	echo "|---|---|---|---|"
} >"$report"

ssh_opts=(-i "$vm/key" -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
	-o ConnectTimeout=5 -o ServerAliveInterval=15 -o ServerAliveCountMax=8)
guest() { ssh "${ssh_opts[@]}" ubuntu@127.0.0.1 "$@"; }
running() { [[ -f $vm/qemu.pid ]] && kill -0 "$(cat "$vm/qemu.pid")" 2>/dev/null; }
monitor() { # COMMAND for QEMU's monitor
	python3 - "$vm/monitor.sock" "$1" <<'PY'
import socket, sys, time
s = socket.socket(socket.AF_UNIX)
s.connect(sys.argv[1])
time.sleep(0.5)
s.recv(65536)  # the monitor's banner
s.sendall(sys.argv[2].encode() + b"\n")
time.sleep(1)
PY
}
boot=""
wait_boot() { # until the guest answers over SSH from a boot other than $boot (ten minutes)
	local _ id
	for _ in $(seq 1 60); do
		id=$(guest cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)
		if [[ -n $id && $id != "$boot" ]]; then boot=$id; return 0; fi
		sleep 5
	done
	return 1
}
phase() { # PHASE [ARGS...] of reboot-guest.sh: its report rows join this report
	local status=0 rows file
	guest "cd /srv/toyota-erp && bash scripts/ci/reboot-guest.sh $(printf '%q ' "$@")" >>"$out/guest.log" 2>&1 || status=$?
	# the phase's own rows file, named by reboot-guest.sh after the phase (and the label of "after")
	file=rows-$1.md
	if [[ $1 == after ]]; then file=rows-after-$(tr 'A-Z ' 'a-z-' <<<"${2:-}" | tr -dc 'a-z0-9-').md; fi
	rows=$(guest "cat ~/reboot/$file" 2>/dev/null || true)
	if [[ -n $rows ]]; then
		printf '%s\n' "$rows" >>"$report"
		failures=$((failures + $(grep -c '\*\*FAILED\*\*' <<<"$rows" || true)))
	else
		echo "| Guest phase: $* | **FAILED** | 0 | no report rows (exit status $status; see guest.log) |" >>"$report"
		failures=$((failures + 1))
	fi
	return "$status"
}
finish() {
	if running; then
		# the guest's logs, the stack's, and the Docker journal of every boot
		guest 'tar -C ~/reboot -cf - .' 2>/dev/null | tar -xf - -C "$out/guest" 2>/dev/null || true
		guest 'cd /srv/toyota-erp && ./scripts/deploy/compose.sh /etc/hinode/production.env logs --no-color' \
			>"$out/compose.log" 2>&1 || true
		guest 'journalctl --list-boots --no-pager; sudo journalctl --no-pager -o short-iso -u containerd.service -u docker.service' \
			>"$out/journal.log" 2>&1 || true
		monitor quit || true
	fi
	{ echo; echo "$failures failed step(s)."; } >>"$report"
	cat "$report"
	exit $((failures > 0))
}
fatal() { fail "$1"; finish; }

# the VM ------------------------------------------------------------------------------------
step "Disposable VM: Ubuntu cloud image under KVM"
[[ -e /dev/kvm ]] || fatal "this host has no /dev/kvm"
sudo chmod 0666 /dev/kvm
if ! { sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
		qemu-system-x86 qemu-utils cloud-image-utils ubuntu-cloudimage-keyring; } >"$out/vm-packages.log" 2>&1; then
	fatal "QEMU could not be installed (see vm-packages.log)"
fi
release=${UBUNTU_CLOUD_IMAGE%/*}
if ! curl -fsSL -o "$vm/SHA256SUMS" "$release/SHA256SUMS" || ! curl -fsSL -o "$vm/SHA256SUMS.gpg" "$release/SHA256SUMS.gpg" \
		|| ! gpgv --keyring /usr/share/keyrings/ubuntu-cloudimage-keyring.gpg "$vm/SHA256SUMS.gpg" "$vm/SHA256SUMS" \
			>"$out/image-signature.log" 2>&1 \
		|| ! grep -q "^$UBUNTU_CLOUD_IMAGE_SHA256 \*${UBUNTU_CLOUD_IMAGE##*/}\$" "$vm/SHA256SUMS"; then
	fatal "the signed SHA256SUMS of $release does not list the pinned image (see image-signature.log)"
fi
curl -fsSL -o "$vm/base.img" "$UBUNTU_CLOUD_IMAGE" || fatal "download of $UBUNTU_CLOUD_IMAGE failed"
sha256sum -c --quiet <<<"$UBUNTU_CLOUD_IMAGE_SHA256  $vm/base.img" || fatal "the image does not match its pinned checksum"
qemu-img create -q -f qcow2 -F qcow2 -b "$vm/base.img" "$vm/disk.qcow2" 64G
ssh-keygen -q -t ed25519 -N '' -C reboot-check -f "$vm/key"
cat >"$vm/user-data" <<EOF
#cloud-config
users:
  - name: ubuntu
    sudo: "ALL=(ALL) NOPASSWD:ALL"
    shell: /bin/bash
    lock_passwd: true
    ssh_authorized_keys: ["$(cat "$vm/key.pub")"]
ssh_pwauth: false
EOF
printf 'instance-id: hinode-erp-reboot\nlocal-hostname: hinode-erp-reboot\n' >"$vm/meta-data"
cloud-localds "$vm/seed.img" "$vm/user-data" "$vm/meta-data"
qemu-system-x86_64 -name hinode-erp-reboot -machine q35,accel=kvm -cpu host -smp 4 -m 12G \
	-drive if=virtio,file="$vm/disk.qcow2",format=qcow2,discard=unmap \
	-drive if=virtio,file="$vm/seed.img",format=raw,readonly=on \
	-netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 -device virtio-net-pci,netdev=net0 \
	-display none -serial file:"$out/console.log" -monitor unix:"$vm/monitor.sock",server=on,wait=off \
	-pidfile "$vm/qemu.pid" -daemonize
wait_boot || fatal "no SSH from the guest within ten minutes (see console.log)"
cloud_init=$(guest 'cloud-init status --wait' 2>&1 | tail -1 || true)
pass "${UBUNTU_CLOUD_IMAGE##*/} of ${release##*/} (signed checksum checked), 4 vCPU, 12 GB, on $(. /etc/os-release && echo "$PRETTY_NAME"); SSH after $((SECONDS - step_started)) s, cloud-init $cloud_init"

step "Repository checkout on the guest (step 2)"
if guest 'sudo install -d -o "$(id -un)" -g "$(id -gn)" /srv/toyota-erp' \
		&& git -C "$REPO_ROOT" archive --format=tar HEAD | guest 'tar -xf - -C /srv/toyota-erp'; then
	pass "$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD) at /srv/toyota-erp"
else
	fatal "the tree could not be copied"
fi

# install by the runbook ----------------------------------------------------------------
# the VM's own package timers off for the test: an unattended upgrade restarting Docker in the
# middle of a check would be noise (the host script waits for package jobs already running)
guest 'sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer' >/dev/null 2>&1 || true
phase host || fatal "the host preparation failed (see guest/host.log)"
step "The application image on the guest"
if docker_cmd save "$image" | gzip -1 | guest 'gunzip | sudo docker load' >"$out/image-load.log" 2>&1; then
	pass "$(tail -1 "$out/image-load.log")"
else
	fatal "see image-load.log"
fi
phase install "$image" || fatal "the install failed (see guest/)"
phase before || true

# a reboot ----------------------------------------------------------------------------------
step "Reboot (systemctl reboot)"
guest 'sudo systemctl reboot' >/dev/null 2>&1 || true
if wait_boot; then
	pass "the guest answered from its new boot after $((SECONDS - step_started)) s"
else
	fatal "the guest did not come back within ten minutes (see console.log)"
fi
phase after Reboot || true

# a power loss ------------------------------------------------------------------------------
phase mark || true
step "Power loss (QEMU system_reset: no shutdown; what the guest had not written is lost)"
monitor system_reset
if wait_boot; then
	pass "the guest answered from its new boot after $((SECONDS - step_started)) s"
else
	fatal "the guest did not come back within ten minutes (see console.log)"
fi
phase after "Power loss" || true
finish
