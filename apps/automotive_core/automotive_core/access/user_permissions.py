"""Managed User Permissions (row-level scope) owned by an access source.

Plant Assignments, Partner User Access and provisioning requests each own a subset
of a user's User Permissions, marked with ``automotive_managed_by``. ``sync`` makes
that subset equal to the desired set without touching permissions owned by another
source or created by hand.
"""

from __future__ import annotations

import frappe


def sync(user: str, source: str, desired: dict[tuple[str, str], bool]) -> None:
	"""desired: {(allow_doctype, value): is_default}"""
	existing = frappe.get_all(
		"User Permission",
		filters={"user": user, "automotive_managed_by": source},
		fields=["name", "allow", "for_value", "is_default"],
	)
	have = {(row.allow, row.for_value): row for row in existing}
	for key, row in have.items():
		if key not in desired:
			frappe.delete_doc("User Permission", row.name, ignore_permissions=True, force=True)
	for (allow, value), is_default in desired.items():
		row = have.get((allow, value))
		if row:
			if bool(row.is_default) != is_default:
				frappe.db.set_value("User Permission", row.name, "is_default", int(is_default))
			continue
		duplicate = frappe.db.exists("User Permission", {"user": user, "allow": allow, "for_value": value})
		if duplicate:
			continue  # an equivalent permission from another source already grants it
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": allow,
				"for_value": value,
				"is_default": int(is_default),
				"apply_to_all_doctypes": 1,
				"automotive_managed_by": source,
			}
		).insert(ignore_permissions=True)
	frappe.cache.hdel("user_permissions", user)


def scoped_values(user: str, allow: str) -> list[str] | None:
	"""Values of ``allow`` the user is restricted to, or None if unrestricted."""
	from frappe.core.doctype.user_permission.user_permission import get_user_permissions

	perms = get_user_permissions(user).get(allow)
	if not perms:
		return None
	return [p.get("doc") for p in perms]


def employee_scope_is_provisioned(doc, method=None) -> None:
	"""Employee before_insert/validate: ERPNext would add a company User Permission for the
	linked user, outside the managed sources above, narrowing group-level roles to one
	company and bypassing provisioning (D-007). Scope comes from provisioning only."""
	doc.create_user_permission = 0
