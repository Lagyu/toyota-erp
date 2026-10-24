"""Helpers for the API contract tests: an HTTP client for a running site (standard library
only, so the tests run without the bench) and a checker for the subset of JSON Schema the
committed OpenAPI document uses."""

from __future__ import annotations

import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
OPENAPI = ROOT / "apps/automotive_core/automotive_core/integration_hub/openapi.json"


def openapi() -> dict[str, Any]:
	return json.loads(OPENAPI.read_text())


class Client:
	"""A session against INTEGRATION_URL; INTEGRATION_HOST names the site behind a shared port."""

	def __init__(self) -> None:
		self.url = os.environ["INTEGRATION_URL"].rstrip("/")
		if urllib.parse.urlsplit(self.url).scheme not in ("http", "https"):
			raise ValueError(f"INTEGRATION_URL must be http(s): {self.url}")
		self.host = os.environ.get("INTEGRATION_HOST")
		self.jar = http.cookiejar.CookieJar()
		self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
		self.csrf_token: str | None = None

	def request(self, method: str, path: str, params: dict | None = None, body: bytes | None = None,
			headers: dict | None = None) -> tuple[int, Any]:
		url = self.url + path + (("?" + urllib.parse.urlencode(params)) if params else "")
		request = urllib.request.Request(url, data=body, method=method, headers={  # noqa: S310 (http(s) only, see __init__)
			"Accept": "application/json", **({"Host": self.host} if self.host else {}),
			**({"X-Frappe-CSRF-Token": self.csrf_token} if self.csrf_token else {}), **(headers or {})})
		try:
			with self.opener.open(request, timeout=60) as response:
				status, raw = response.status, response.read()
		except urllib.error.HTTPError as exc:
			with exc:
				status, raw = exc.code, exc.read()
		try:
			return status, json.loads(raw or b"null")
		except ValueError:
			return status, raw.decode(errors="replace")

	def login(self, user: str, password: str) -> None:
		body = urllib.parse.urlencode({"usr": user, "pwd": password}).encode()
		status, data = self.request("POST", "/api/method/login", body=body,
			headers={"Content-Type": "application/x-www-form-urlencoded"})
		if status != 200:
			message = data.get("message") or data.get("exc_type") if isinstance(data, dict) else str(data)[:200]
			raise AssertionError(f"login as {user} failed ({status}: {message})")


	def csrf(self, path: str) -> None:
		"""The session's CSRF token from a page (the desk or a portal), sent with every later
		request: Frappe refuses state-changing calls of a cookie session without it."""
		status, page = self.request("GET", path, headers={"Accept": "text/html"})
		match = re.search(r'csrf_token\s*=\s*"([^"]+)"', page if isinstance(page, str) else "")
		if status != 200 or not match:
			raise AssertionError(f"no CSRF token on {path} ({status})")
		self.csrf_token = match.group(1)


def sign(secret: str, body: bytes) -> str:
	"""X-Hinode-Signature for a partner delivery (automotive_core.integration_hub.signing):
	every call has a fresh nonce, as a partner's retry would."""
	timestamp, nonce = int(time.time()), secrets.token_hex(16)
	digest = hmac.new(secret.encode(), f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256).hexdigest()
	return f"t={timestamp},kid=integration-tests,n={nonce},v1={digest}"


def encode_edi(envelope: dict) -> str:
	"""Hinode EDI as a partner's system writes it (format: integration_hub/adapters/edi.py;
	the same encoder as e2e/support/partner.ts)."""

	def escape(value: str) -> str:
		return value.replace("?", "??").replace("*", "?*").replace("~", "?~")

	def flatten(prefix: str, value: Any, out: list) -> None:
		if value is None:
			out.append([prefix, "Z", ""])
		elif isinstance(value, list):
			if not value:
				out.append([prefix, "L", ""])
			for i, item in enumerate(value):
				flatten(f"{prefix}.{i}", item, out)
		elif isinstance(value, dict):
			if not value:
				out.append([prefix, "O", ""])
			for key in sorted(value):
				flatten(f"{prefix}.{key}" if prefix else key, value[key], out)
		elif isinstance(value, bool):
			out.append([prefix, "B", "1" if value else "0"])
		elif isinstance(value, int | float):
			out.append([prefix, "N", str(value)])
		else:
			out.append([prefix, "S", str(value)])

	control = hashlib.sha256(envelope["message_id"].encode()).hexdigest()[:9]
	leaves: list = []
	flatten("", envelope.get("data") or {}, leaves)
	segments = [["ISA", "HINODE", "1", control], ["ST", envelope["message_type"], envelope["message_id"]],
		["BGN", envelope.get("schema_version", "v1"), envelope.get("sent_at", "")],
		*(["DTA", path, kind, value] for path, kind, value in leaves)]
	segments.append(["SE", str(len(segments)), envelope["message_id"]])
	segments.append(["IEA", "1", control])
	return "".join("*".join(escape(part) for part in segment) + "~\n" for segment in segments)


def resolve(spec: dict, node: Any) -> Any:
	while isinstance(node, dict) and "$ref" in node:
		target = spec
		for part in node["$ref"].lstrip("#/").split("/"):
			target = target[part]
		node = target
	return node


TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def problems(spec: dict, schema: Any, value: Any, where: str = "$") -> list[str]:
	"""Where ``value`` breaks ``schema`` (type, properties, required, items, oneOf/anyOf, $ref)."""
	schema = resolve(spec, schema)
	if not isinstance(schema, dict) or not schema:
		return []
	for key in ("oneOf", "anyOf"):
		if key in schema:
			options = [problems(spec, option, value, where) for option in schema[key]]
			return [] if any(not o for o in options) else [f"{where}: matches none of {key}"]
	kind = schema.get("type")
	if kind in ("integer", "number"):
		if isinstance(value, bool) or not isinstance(value, int | float):
			return [f"{where}: expected {kind}, got {type(value).__name__}"]
	elif kind in TYPES and not isinstance(value, TYPES[kind]):
		if not (schema.get("nullable") and value is None):
			return [f"{where}: expected {kind}, got {type(value).__name__}"]
	found = []
	if isinstance(value, dict):
		found += [f"{where}: missing {key!r}" for key in schema.get("required", []) if key not in value]
		for key, sub in (schema.get("properties") or {}).items():
			if key in value:
				found += problems(spec, sub, value[key], f"{where}.{key}")
	if isinstance(value, list) and "items" in schema:
		for i, item in enumerate(value[:20]):
			found += problems(spec, schema["items"], item, f"{where}[{i}]")
	return found
