"""HMAC-SHA256 request signing with timestamp + nonce replay protection (ERP-11).

Header format (both directions)::

    X-Hinode-Signature: t=<unix seconds>,kid=<key id>,n=<nonce>,v1=<hex hmac>

where ``v1 = HMAC_SHA256(secret, f"{t}.{n}.".encode() + body)``. The receiver rejects
signatures outside the timestamp tolerance and nonces it has already seen for that
partner (stored durably in Integration Nonce until the tolerance window passes).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

HEADER = "X-Hinode-Signature"
PARTNER_HEADER = "X-Hinode-Partner"


@dataclass(frozen=True)
class Signature:
	timestamp: int
	key_id: str
	nonce: str
	digest: str


class SignatureError(Exception):
	"""Raised with a short, non-sensitive reason."""


def compute(secret: str, body: bytes, timestamp: int, nonce: str) -> str:
	message = f"{timestamp}.{nonce}.".encode() + body
	return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def build_header(
	key_id: str, secret: str, body: bytes, timestamp: int | None = None, nonce: str | None = None
) -> str:
	timestamp = int(time.time()) if timestamp is None else timestamp
	nonce = nonce or secrets.token_hex(16)
	return f"t={timestamp},kid={key_id},n={nonce},v1={compute(secret, body, timestamp, nonce)}"


def parse_header(value: str | None) -> Signature:
	if not value:
		raise SignatureError("missing signature header")
	parts: dict[str, str] = {}
	for item in value.split(","):
		key, sep, val = item.strip().partition("=")
		if sep:
			parts[key] = val
	try:
		return Signature(int(parts["t"]), parts.get("kid", ""), parts["n"], parts["v1"])
	except (KeyError, ValueError) as exc:
		raise SignatureError("malformed signature header") from exc


def verify(
	header: str | None, body: bytes, secret: str, tolerance_seconds: int, now: int | None = None
) -> Signature:
	sig = parse_header(header)
	now = int(time.time()) if now is None else now
	if abs(now - sig.timestamp) > tolerance_seconds:
		raise SignatureError("timestamp outside tolerance")
	if not (8 <= len(sig.nonce) <= 64) or not sig.nonce.isalnum():
		raise SignatureError("invalid nonce")
	expected = compute(secret, body, sig.timestamp, sig.nonce)
	if not hmac.compare_digest(expected, sig.digest):
		raise SignatureError("signature mismatch")
	return sig
