"""Authorization at the API (GATE-4, SEC-3, P-4), as signed-in users without the right:

- every method that names the roles it requires (``x-roles`` in the contract) refuses a line
  operator who holds none of them, and a partner portal user;
- every partner-portal method (``x-party``) refuses the other kind of partner: a dealer on the
  supplier portal's methods, a supplier on the dealer portal's.

Each call carries placeholder arguments of the documented types and an Idempotency-Key, so the
refusal is the method's own check and not a missing argument. A refusal is a 403, or a 404 for
a method that must load the named document to know which role it needs. Run by
scripts/ci/contract.sh with the run's personas (INTEGRATION_CREDENTIALS); skipped without them.
"""

from __future__ import annotations

import ast
import json
import os
import unittest
import uuid
from pathlib import Path

from integration_tests.contract import ROOT, Client, openapi, resolve

SPEC = openapi()
PLACEHOLDERS = {"string": "x", "integer": 0, "number": 0, "boolean": False, "array": [], "object": {}}
AUTOMATIC_ROLES = {"All", "Guest", "Desk User"}
REFUSED = (403, 404)


def role_profiles() -> dict[str, list[str]]:
	"""ROLE_PROFILES of automotive_core.setup.roles, read without importing Frappe."""
	tree = ast.parse((ROOT / "apps/automotive_core/automotive_core/setup/roles.py").read_text())
	for node in tree.body:
		if isinstance(node, ast.AnnAssign | ast.Assign):
			target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
			if isinstance(target, ast.Name) and target.id == "ROLE_PROFILES":
				return ast.literal_eval(node.value)
	raise LookupError("ROLE_PROFILES not found")


def operations():
	for path, item in sorted(SPEC["paths"].items()):
		for method, op in sorted(item.items()):
			if op.get("security") != []:  # guest methods have their own tests
				yield path, method.upper(), op


def placeholder(schema):
	schema = resolve(SPEC, schema or {})
	for key in ("oneOf", "anyOf"):
		if key in schema:
			options = [s for s in schema[key] if resolve(SPEC, s).get("type") != "null"]
			return placeholder(options[0]) if options else None
	kind = schema.get("type", "string")
	if isinstance(kind, list):
		kind = next((k for k in kind if k != "null"), "string")
	return PLACEHOLDERS.get(kind, "x")


def call(client: Client, path: str, method: str, op: dict):
	headers = {"Idempotency-Key": str(uuid.uuid4())}
	if method in ("GET", "DELETE"):
		params = [resolve(SPEC, p) for p in op.get("parameters", [])]
		query = {p["name"]: placeholder(p.get("schema")) for p in params if p.get("in") == "query" and p.get("required")}
		return client.request(method, path, params=query or None, headers=headers)
	schema = resolve(SPEC, next(iter(op.get("requestBody", {}).get("content", {}).values()), {}).get("schema", {}))
	body = {name: placeholder(prop) for name, prop in schema.get("properties", {}).items()
		if name in schema.get("required", [])}
	return client.request(method, path, body=json.dumps(body).encode(),
		headers={**headers, "Content-Type": "application/json"})


class TestAuthorization(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		path = os.environ.get("INTEGRATION_CREDENTIALS")
		if not path or not Path(path).exists():
			raise unittest.SkipTest("no personas (INTEGRATION_CREDENTIALS)")
		credentials = json.loads(Path(path).read_text())
		cls.clients = {}
		for persona, page in (("operator", "/desk"), ("supplier", "/supplier-portal"), ("dealer", "/dealer-portal")):
			user = credentials["users"].get(persona)
			if not user:
				raise unittest.SkipTest(f"no {persona} persona")
			client = Client()
			client.login(user, credentials["password"])
			client.csrf(page)
			cls.clients[persona] = client
		cls.operator_roles = set(role_profiles()["Line Operator"]) | AUTOMATIC_ROLES

	def refusals(self, persona: str, ops: list) -> list[str]:
		wrong = []
		for path, method, op in ops:
			status, data = call(self.clients[persona], path, method, op)
			if status not in REFUSED:
				wrong.append(f"{persona}: {method} {path}: {status} {str(data)[:160]}")
		return wrong

	def test_methods_refuse_users_without_the_roles_they_name(self):
		guarded = [o for o in operations() if o[2].get("x-roles")]
		self.assertGreater(len(guarded), 20)
		wrong = self.refusals("operator", [o for o in guarded if not set(o[2]["x-roles"]) & self.operator_roles])
		wrong += self.refusals("dealer", guarded)
		self.assertEqual(wrong, [])

	def test_portal_methods_refuse_the_other_kind_of_partner(self):
		wrong = []
		for persona, other in (("dealer", "Supplier"), ("supplier", "Customer")):
			ops = [o for o in operations() if o[2].get("x-party") == other]
			self.assertGreater(len(ops), 10, other)
			wrong += self.refusals(persona, ops)
		self.assertEqual(wrong, [])


if __name__ == "__main__":
	unittest.main()
