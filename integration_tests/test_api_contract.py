"""The running API against the committed OpenAPI contract (TST-4, ERP-11.1).

- Every documented method exists, and refuses guests with the documented 403.
- Every read without required parameters answers with a documented status; a 200 body
  matches its schema.
- The signed inbound endpoint answers unsigned, unknown and badly signed deliveries with 401.

Run against a reference site with scripts/ci/contract.sh.
"""

from __future__ import annotations

import json
import os
import unittest

from integration_tests.contract import Client, openapi, problems, resolve

SPEC = openapi()


def operations():
	for path, item in sorted(SPEC["paths"].items()):
		for method, op in sorted(item.items()):
			yield path, method.upper(), op


def guest_callable(op: dict) -> bool:
	return op.get("security") is not None


def required_params(op: dict) -> list[dict]:
	return [p for p in (resolve(SPEC, p) for p in op.get("parameters", [])) if p.get("required")]


class TestContract(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.guest = Client()
		cls.user = Client()
		cls.user.login(
			os.environ.get("INTEGRATION_USER", "Administrator"), os.environ["INTEGRATION_PASSWORD"]
		)

	def test_every_method_exists_and_refuses_guests(self):
		wrong = []
		for path, method, op in operations():
			if guest_callable(op):
				continue
			body = b"{}" if method == "POST" else None
			status, data = self.guest.request(
				method, path, body=body, headers={"Content-Type": "application/json"} if body else None
			)
			if status != 403:
				wrong.append(f"{method} {path}: {status} {str(data)[:120]}")
		self.assertEqual(wrong, [])

	def test_reads_answer_as_documented(self):
		wrong, checked = [], 0
		for path, method, op in operations():
			if method != "GET" or guest_callable(op) or required_params(op):
				continue
			status, data = self.user.request("GET", path)
			checked += 1
			if str(status) not in op["responses"]:
				wrong.append(f"GET {path}: undocumented status {status} {str(data)[:120]}")
				continue
			if status == 200:
				schema = (
					resolve(SPEC, op["responses"]["200"])
					.get("content", {})
					.get("application/json", {})
					.get("schema")
				)
				wrong += [f"GET {path}: {p}" for p in problems(SPEC, schema, data)]
		self.assertGreater(checked, 5)
		self.assertEqual(wrong, [])

	def test_inbound_endpoint_refuses_unauthenticated_deliveries(self):
		path = "/api/method/automotive_core.integration_hub.inbound.receive"
		self.assertIn("401", SPEC["paths"][path]["post"]["responses"])
		envelope = json.dumps({"message_id": "CONTRACT-1", "message_type": "asn", "data": {}}).encode()
		for headers in (
			{},
			{"X-Hinode-Partner": "NO-SUCH-PARTNER"},
			{"X-Hinode-Partner": "NO-SUCH-PARTNER", "X-Hinode-Signature": "t=1,kid=k,n=x,v1=00"},
		):
			status, data = self.guest.request(
				"POST", path, body=envelope, headers={"Content-Type": "application/json", **headers}
			)
			self.assertEqual(status, 401, f"{headers}: {status} {str(data)[:120]}")


if __name__ == "__main__":
	unittest.main()
