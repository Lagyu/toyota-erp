"""GATE-4: the documented permission matrix (ops/security/permission-matrix.md) holds in the
live permission tables, and company scope keeps another company's documents out of the
desk lists and the REST API."""

from __future__ import annotations

from pathlib import Path

import frappe
from frappe.permissions import get_role_permissions
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from automotive_core.access import user_permissions
from automotive_core.setup.roles import ROLE_PROFILES
from automotive_core.tests.utils import AICHI, KYUSHU, as_user, make_user

RIGHTS = (("read", "r"), ("write", "w"), ("create", "c"), ("submit", "s"), ("cancel", "x"))


def matrix_file() -> Path | None:
	"""The table in the repository (the app is linked into the development bench from it)."""
	path = Path(frappe.get_app_path("automotive_core")).resolve().parents[2] / "ops/security/permission-matrix.md"
	return path if path.exists() else None


def parse(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
	rows = [[c.strip() for c in line.strip().strip("|").split("|")] for line in path.read_text().splitlines()
		if line.startswith("| ")]
	header, body = rows[0], [r for r in rows[1:] if not set(r[0]) <= {"-"}]
	personas = header[1:]
	return personas, {r[0]: dict(zip(personas, r[1:], strict=True)) for r in body}


class TestPermissionMatrix(IntegrationTestCase):
	def test_personas_hold_exactly_the_documented_rights(self):
		path = matrix_file()
		if not path:
			self.skipTest("the permission matrix is documented in the repository, not in an image")
		personas, matrix = parse(path)
		self.assertEqual(sorted(personas), sorted(p for p in ROLE_PROFILES if "Portal" not in p))
		users = {p: make_user(f"matrix.{frappe.scrub(p)}@hinode.example", ROLE_PROFILES[p]) for p in personas}
		wrong = []
		for doctype, documented in matrix.items():
			for persona, rights in documented.items():
				user = users[persona]
				granted = "".join(code for ptype, code in RIGHTS if frappe.has_permission(doctype, ptype, user=user)) or "-"
				# rights the roles grant only on documents the user created ("if owner")
				own = get_role_permissions(frappe.get_meta(doctype), user=user, is_owner=True)
				owned = "".join(code for ptype, code in RIGHTS if own.get("if_owner", {}).get(ptype) and code not in granted)
				actual = f"{granted}/{owned}" if owned else granted
				if actual != rights:
					wrong.append(f"{doctype} / {persona}: documented {rights}, actual {actual}")
		self.assertEqual(wrong, [])

	def _journal(self, company: str):
		def account(**filters):
			return frappe.db.get_value("Account", {"company": company, "is_group": 0, "disabled": 0, **filters}, "name")

		cash = account(account_type="Cash")
		expense = account(root_type="Expense", account_type="")
		cost_center = frappe.get_cached_value("Company", company, "cost_center")
		return frappe.get_doc({"doctype": "Journal Entry", "company": company, "posting_date": nowdate(),
			"accounts": [{"account": expense, "debit_in_account_currency": 100, "cost_center": cost_center},
				{"account": cash, "credit_in_account_currency": 100, "cost_center": cost_center}]}).insert(
			ignore_permissions=True)

	def test_company_scope_in_desk_lists_and_the_api(self):
		user = make_user("matrix.ap.kyushu@hinode.example", ROLE_PROFILES["AP Clerk"])
		user_permissions.sync(user, "test-permission-matrix", {("Company", KYUSHU): True})
		own, other = self._journal(KYUSHU), self._journal(AICHI)
		self.assertTrue(frappe.has_permission("Journal Entry", "read", own, user=user))
		self.assertFalse(frappe.has_permission("Journal Entry", "read", other, user=user))
		with as_user(user):
			listed = frappe.get_list("Journal Entry", filters={"name": ("in", [own.name, other.name])}, pluck="name")
			self.assertEqual(listed, [own.name])
			self.assertEqual(frappe.client.get("Journal Entry", own.name)["name"], own.name)
			with self.assertRaises(frappe.PermissionError):
				frappe.client.get("Journal Entry", other.name)
