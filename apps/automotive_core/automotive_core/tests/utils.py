"""Helpers for integration tests on the test site (real MariaDB/Redis, real ERPNext)."""

from __future__ import annotations

import contextlib

import frappe

AICHI = "Hinode Automotive Manufacturing Japan"
KYUSHU = "Hinode Powertrain Components"
NA = "Hinode Automotive Distribution North America"
TEST_VOUCHER = "AC Test Voucher"


def ensure_test_doctypes() -> None:
	"""A small submittable custom DocType used to exercise the approval engine in
	isolation from ERPNext's own validations. Created once per test site (DDL commits)."""
	if not frappe.db.exists("DocType", f"{TEST_VOUCHER} Item"):
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": f"{TEST_VOUCHER} Item",
				"module": "Governance",
				"custom": 1,
				"istable": 1,
				"fields": [
					{"fieldname": "item_code", "fieldtype": "Data", "label": "Item"},
					{"fieldname": "qty", "fieldtype": "Float", "label": "Qty"},
					{"fieldname": "rate", "fieldtype": "Currency", "label": "Rate"},
					{"fieldname": "item_group", "fieldtype": "Data", "label": "Item Group"},
				],
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("DocType", TEST_VOUCHER):
		frappe.get_doc(
			{
				"doctype": "DocType",
				"name": TEST_VOUCHER,
				"module": "Governance",
				"custom": 1,
				"is_submittable": 1,
				"autoname": "hash",
				"fields": [
					{"fieldname": "company", "fieldtype": "Link", "options": "Company", "label": "Company"},
					{"fieldname": "party", "fieldtype": "Data", "label": "Party"},
					{"fieldname": "grand_total", "fieldtype": "Currency", "label": "Total"},
					{"fieldname": "purpose", "fieldtype": "Data", "label": "Purpose"},
					{"fieldname": "items", "fieldtype": "Table", "options": f"{TEST_VOUCHER} Item", "label": "Items"},
					{"fieldname": "amended_from", "fieldtype": "Link", "options": TEST_VOUCHER, "label": "Amended From",
					 "read_only": 1, "no_copy": 1},
				],
				"permissions": [
					{"role": "System Manager", "read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1, "amend": 1,
					 "delete": 1},
					{"role": "Buyer", "read": 1, "write": 1, "create": 1, "submit": 1, "cancel": 1, "amend": 1},
					{"role": "Procurement Manager", "read": 1},
					{"role": "Plant Director", "read": 1},
					{"role": "Treasury Approver", "read": 1},
				],
			}
		).insert(ignore_permissions=True)


def make_user(email: str, roles: list[str], user_type: str = "System User", first_name: str | None = None) -> str:
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
		user.set("roles", [])
		user.append_roles(*roles)
		user.flags.ignore_permissions = True
		user.save()
		return email
	user = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": first_name or email.split("@")[0],
			"user_type": user_type,
			"send_welcome_email": 0,
			"enabled": 1,
		}
	)
	user.append_roles(*roles)
	user.flags.ignore_permissions = True
	user.insert()
	return email


@contextlib.contextmanager
def as_user(user: str):
	previous = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)


def make_voucher(owner: str, total: float, company: str = AICHI, items: list[dict] | None = None, **extra):
	with as_user(owner):
		doc = frappe.get_doc(
			{
				"doctype": TEST_VOUCHER,
				"company": company,
				"party": extra.pop("party", "Kisaragi Precision"),
				"grand_total": total,
				"items": items or [{"item_code": "BRK-PAD-001", "qty": 10, "rate": total / 10, "item_group": "Brakes"}],
				**extra,
			}
		)
		doc.insert()
	return doc


def make_policy(name: str, steps: list[tuple[int, str, float]], **extra) -> str:
	if frappe.db.exists("Approval Policy", name):
		frappe.delete_doc("Approval Policy", name, force=True, ignore_permissions=True)
	doc = frappe.get_doc(
		{
			"doctype": "Approval Policy",
			"policy_name": name,
			"reference_doctype": TEST_VOUCHER,
			"is_active": 1,
			"amount_field": "grand_total",
			"maker_cannot_approve": 1,
			"distinct_approvers": 1,
			"allow_delegation": 1,
			"bind_to_document_hash": 1,
			"steps": [{"step_no": n, "step_name": f"Step {n}", "approver_role": role, "min_amount": minimum}
				for n, role, minimum in steps],
			**extra,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def approve(doc, maker: str, approvers: list[str]) -> dict:
	"""Run a full approval cycle: maker requests, each approver decides (distinct people)."""
	from automotive_core.governance import approvals

	with as_user(maker):
		req = approvals.request_approval(doc.doctype, doc.name)
	for approver in approvers:
		with as_user(approver):
			req = approvals.decide(req["name"], "Approve")
	if req["status"] != "Approved":
		raise AssertionError(f"approval did not complete: {req}")
	return req
