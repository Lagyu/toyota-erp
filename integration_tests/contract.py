"""Helpers for the API contract tests: an HTTP client for a running site (standard library
only, so the tests run without the bench) and a checker for the subset of JSON Schema the
committed OpenAPI document uses."""

from __future__ import annotations

import http.cookiejar
import json
import os
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

	def request(self, method: str, path: str, params: dict | None = None, body: bytes | None = None,
			headers: dict | None = None) -> tuple[int, Any]:
		url = self.url + path + (("?" + urllib.parse.urlencode(params)) if params else "")
		request = urllib.request.Request(url, data=body, method=method, headers={  # noqa: S310 (http(s) only, see __init__)
			"Accept": "application/json", **({"Host": self.host} if self.host else {}), **(headers or {})})
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
		status, _ = self.request("POST", "/api/method/login", body=body,
			headers={"Content-Type": "application/x-www-form-urlencoded"})
		if status != 200:
			raise AssertionError(f"login as {user} failed ({status})")


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
