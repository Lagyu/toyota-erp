"""Persona roles, role profiles and privileged-role MFA flags (ERP-10).

Each persona is a Role Profile = one custom persona role + the ERPNext roles it
needs. Custom persona roles carry the permissions on our own DocTypes (declared in
the DocType specs); ERPNext roles carry the standard DocTypes. Additional least-
privilege grants on standard DocTypes live in setup/permissions.py.
"""

from __future__ import annotations

import frappe

INTERNAL_ROLES = [
	"Automotive Executive",
	"Group Controller",
	"Plant Director",
	"Production Planner",
	"Production Supervisor",
	"Line Operator",
	"Warehouse Clerk",
	"Quality Inspector",
	"Buyer",
	"Procurement Manager",
	"AP Clerk",
	"Treasury Approver",
	"Dealer Liaison",
	"IT Operator",
	"Product Engineer",
	"Engineering Manager",
	"Logistics Coordinator",
]
# Shipped by ERPNext and reused as-is.
REUSED_ROLES = ["Auditor", "Quality Manager", "Sales Manager"]
PORTAL_ROLES = ["Supplier Portal User", "Dealer Portal User"]

# Roles whose holders must use two-factor authentication when it is enabled in
# System Settings (production tenant configuration enables it).
PRIVILEGED_ROLES = ["System Manager", "IT Operator", "Treasury Approver", "Group Controller", "Automotive Executive"]

ROLE_PROFILES: dict[str, list[str]] = {
	"Automotive Executive": ["Automotive Executive"],
	"Group Controller": ["Group Controller", "Accounts Manager"],
	"Plant Director": ["Plant Director", "Manufacturing Manager", "Stock User"],
	"Production Planner": ["Production Planner", "Manufacturing User", "Stock User"],
	"Production Supervisor": ["Production Supervisor", "Manufacturing User"],
	"Line Operator": ["Line Operator"],
	"Warehouse Clerk": ["Warehouse Clerk", "Stock User"],
	"Quality Inspector": ["Quality Inspector"],
	"Quality Manager": ["Quality Manager"],
	"Buyer": ["Buyer", "Purchase User"],
	"Procurement Manager": ["Procurement Manager", "Purchase Manager"],
	"AP Clerk": ["AP Clerk", "Accounts User"],
	"Treasury Approver": ["Treasury Approver"],
	"Sales Manager": ["Sales Manager", "Sales User"],
	"Dealer Liaison": ["Dealer Liaison", "Sales User"],
	"IT Operator": ["IT Operator", "System Manager"],
	"Auditor": ["Auditor"],
	"Product Engineer": ["Product Engineer"],
	"Engineering Manager": ["Engineering Manager", "Manufacturing Manager"],
	"Logistics Coordinator": ["Logistics Coordinator", "Stock User", "Delivery User"],
	"Supplier Portal User": ["Supplier Portal User", "Supplier"],
	"Dealer Portal User": ["Dealer Portal User", "Customer"],
}


def ensure_roles() -> None:
	for role in INTERNAL_ROLES:
		_ensure_role(role, desk_access=1)
	for role in PORTAL_ROLES:
		_ensure_role(role, desk_access=0)
	for role in PRIVILEGED_ROLES:
		if frappe.db.exists("Role", role):
			frappe.db.set_value("Role", role, "two_factor_auth", 1, update_modified=False)


def _ensure_role(name: str, desk_access: int) -> None:
	if frappe.db.exists("Role", name):
		frappe.db.set_value("Role", name, "desk_access", desk_access, update_modified=False)
		return
	frappe.get_doc({"doctype": "Role", "role_name": name, "desk_access": desk_access}).insert(
		ignore_permissions=True
	)


def ensure_role_profiles() -> None:
	for profile, roles in ROLE_PROFILES.items():
		if frappe.db.exists("Role Profile", profile):
			doc = frappe.get_doc("Role Profile", profile)
			current = {r.role for r in doc.roles}
			if current == set(roles):
				continue
			doc.set("roles", [])
		else:
			doc = frappe.new_doc("Role Profile")
			doc.role_profile = profile
		for role in roles:
			doc.append("roles", {"role": role})
		doc.save(ignore_permissions=True)
