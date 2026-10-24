"""Segregation of duties: conflicting role pairs on one account (ERP-09, ERP-10)."""

from __future__ import annotations

import frappe
from frappe import _

from automotive_core.governance import audit


class SegregationOfDutiesError(frappe.ValidationError):
	pass


def conflicts(roles: set[str]) -> list[dict]:
	rules = frappe.get_all(
		"Segregation of Duties Rule",
		filters={"is_active": 1},
		fields=["name", "role_a", "role_b", "enforcement", "rationale"],
	)
	return [r for r in rules if r.role_a in roles and r.role_b in roles]


def validate_user_roles(doc, method=None) -> None:
	if doc.name in ("Administrator", "Guest") or frappe.flags.in_install:
		return
	roles = {r.role for r in doc.get("roles") or []}
	found = conflicts(roles)
	if not found:
		return
	blocking = [r for r in found if r.enforcement == "Block"]
	if blocking:
		raise SegregationOfDutiesError(
			_("Segregation of duties: {0} cannot hold both {1}.").format(
				doc.name, "; ".join(f"{r.role_a} + {r.role_b} ({r.name})" for r in blocking)
			)
		)
	for rule in found:
		frappe.msgprint(
			_("Segregation of duties warning ({0}): {1} and {2}. {3}").format(
				rule.name, rule.role_a, rule.role_b, rule.rationale
			),
			indicator="orange",
			alert=True,
		)
	doc.flags.automotive_sod_warnings = [r.name for r in found]


def record_warnings(doc, method=None) -> None:
	"""on_update: audit accepted warnings once the user record exists."""
	for rule in doc.flags.get("automotive_sod_warnings") or []:
		audit.record("sod.warning_accepted", "User", doc.name, {"rule": rule})
	doc.flags.automotive_sod_warnings = []


@frappe.whitelist()
def report() -> list[dict]:
	"""Every enabled user that currently violates an active rule (for auditors)."""
	frappe.only_for(("Auditor", "System Manager", "IT Operator", "Group Controller"))
	users = frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, pluck="name")
	out = []
	for user in users:
		roles = set(frappe.get_roles(user))
		for rule in conflicts(roles):
			out.append(
				{
					"user": user,
					"rule": rule.name,
					"role_a": rule.role_a,
					"role_b": rule.role_b,
					"enforcement": rule.enforcement,
				}
			)
	return out
