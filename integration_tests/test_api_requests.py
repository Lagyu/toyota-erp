"""State-changing methods against their documented request schema (TST-4).

Each POST method of the committed OpenAPI document is called by the administrator with a
JSON body of the documented shape (every documented property, placeholder values) whose
references name nothing that exists. It must answer with a documented status, a refusal
(404, 417, 409, 422, 429) rather than a server error, and a 200 body must match the
documented response schema.

The calls can leave records named after the placeholders: run this module on a disposable
stack, after the browser suite (scripts/ci/e2e.sh does).
"""

from __future__ import annotations

import json
import os
import unittest

from integration_tests.contract import Client, openapi, problems, resolve

SPEC = openapi()
PLACEHOLDER = "CONTRACT-PLACEHOLDER"


def placeholder(schema: dict):
	"""A value of the schema's type that names nothing: the placeholder string, zero, false,
	an empty object or list; for oneOf/anyOf the first non-null option."""
	schema = resolve(SPEC, schema) or {}
	for key in ("oneOf", "anyOf"):
		if key in schema:
			options = [resolve(SPEC, o) for o in schema[key]]
			usable = [o for o in options if o.get("type") != "null"] or options
			return placeholder(usable[0])
	return {
		"string": PLACEHOLDER,
		"integer": 0,
		"number": 0,
		"boolean": False,
		"object": {},
		"array": [],
	}.get(schema.get("type"), PLACEHOLDER)


def cause(data) -> str:
	"""The exception a failed call reports: the last line of its traceback, else its type."""
	if isinstance(data, dict):
		if data.get("exc"):
			try:
				lines = [ln for ln in json.loads(data["exc"])[0].splitlines() if ln.strip()]
				return lines[-1][:200]
			except (ValueError, IndexError, TypeError):
				pass
		return str(data.get("exc_type") or data.get("exception") or data)[:200]
	return str(data)[:200]


def state_changing():
	for path, item in sorted(SPEC["paths"].items()):
		op = item.get("post")
		# the signed inbound endpoint is a partner's, tested in test_api_contract
		if op and op.get("security") is None:
			yield path, op


class TestRequests(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.user = Client()
		cls.user.login(
			os.environ.get("INTEGRATION_USER", "Administrator"), os.environ["INTEGRATION_PASSWORD"]
		)
		cls.user.csrf("/app")

	def test_state_changing_methods_refuse_requests_naming_nothing(self):
		wrong, checked = [], 0
		for path, op in state_changing():
			schema = resolve(SPEC, op["requestBody"])["content"]["application/json"]["schema"]
			body = {
				name: placeholder(sub)
				for name, sub in (resolve(SPEC, schema).get("properties") or {}).items()
			}
			status, data = self.user.request(
				"POST", path, body=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
			)
			checked += 1
			if str(status) not in op["responses"]:
				wrong.append(f"POST {path}: undocumented status {status} {cause(data)}")
			elif status == 200:
				response = resolve(SPEC, op["responses"]["200"])
				schema = response.get("content", {}).get("application/json", {}).get("schema")
				wrong += [f"POST {path}: {p}" for p in problems(SPEC, schema, data)]
		self.assertGreater(checked, 50)
		self.assertEqual(wrong, [], "\n" + "\n".join(wrong))


if __name__ == "__main__":
	unittest.main()
