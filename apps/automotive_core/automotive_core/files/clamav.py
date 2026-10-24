"""Minimal clamd INSTREAM client (https://docs.clamav.net/manual/Usage/Scanning.html#clamd).

Used only when a clamd host is configured in Automotive Settings; the production
compose file can add a clamav service (infra/compose/overrides/clamav.yaml).
"""

from __future__ import annotations

import socket
import struct

CHUNK = 64 * 1024


class ClamdError(Exception):
	pass


def scan_bytes(data: bytes, host: str, port: int = 3310, timeout: float = 30.0) -> tuple[bool, str | None]:
	"""Return (clean, signature). Raises ClamdError when clamd is unreachable or errors."""
	try:
		with socket.create_connection((host, port), timeout=timeout) as sock:
			sock.sendall(b"zINSTREAM\0")
			for offset in range(0, len(data), CHUNK):
				chunk = data[offset : offset + CHUNK]
				sock.sendall(struct.pack("!L", len(chunk)) + chunk)
			sock.sendall(struct.pack("!L", 0))
			reply = b""
			while not reply.endswith(b"\0"):
				part = sock.recv(4096)
				if not part:
					break
				reply += part
	except OSError as exc:
		raise ClamdError(f"clamd unreachable: {exc}") from exc
	text = reply.rstrip(b"\0").decode(errors="replace")
	if text.endswith("OK"):
		return True, None
	if text.endswith("FOUND"):
		return False, text.split(":", 1)[-1].replace("FOUND", "").strip()
	raise ClamdError(f"unexpected clamd reply: {text}")


def ping(host: str, port: int = 3310, timeout: float = 3.0) -> bool:
	"""clamd answers PING with PONG (liveness for the metrics endpoint)."""
	try:
		with socket.create_connection((host, port), timeout=timeout) as sock:
			sock.sendall(b"zPING\0")
			return sock.recv(16).rstrip(b"\0") == b"PONG"
	except OSError:
		return False
