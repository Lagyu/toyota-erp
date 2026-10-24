"""End-to-end test fixtures on reference sites (never production).

``prepare_users`` picks one user of the synthetic dataset per persona and gives them a
password for this test run only (returned once, never stored), so the Playwright suite
logs in as real dataset users with their real roles and row-level scopes.

    bench --site SITE execute automotive_seed.e2e.prepare_users
"""

from __future__ import annotations

import json

import frappe
from frappe.utils.password import update_password

from automotive_seed.runner import guard

# persona key -> (role, preferred plant or None for group-level staff)
PERSONAS = {
	"executive": ("Automotive Executive", None),
	"controller": ("Group Controller", None),
	"buyer": ("Buyer", None),
	"procurement_manager": ("Procurement Manager", None),
	"ap_clerk": ("AP Clerk", None),
	"treasury": ("Treasury Approver", None),
	"sales_manager": ("Sales Manager", None),
	"dealer_liaison": ("Dealer Liaison", None),
	"it_operator": ("IT Operator", None),
	"auditor": ("Auditor", None),
	"plant_director": ("Plant Director", "AICHI-VA"),
	"planner": ("Production Planner", "AICHI-VA"),
	"supervisor": ("Production Supervisor", "AICHI-VA"),
	"operator": ("Line Operator", "AICHI-VA"),
	"warehouse": ("Warehouse Clerk", "AICHI-VA"),
	"inspector": ("Quality Inspector", "AICHI-VA"),
	"quality_manager": ("Quality Manager", "AICHI-VA"),
	"engineer": ("Product Engineer", "AICHI-VA"),
	"engineering_manager": ("Engineering Manager", "AICHI-VA"),
	"logistics": ("Logistics Coordinator", "AICHI-VA"),
	"na_warehouse": ("Warehouse Clerk", "NA-PDC"),
}


def _user_with(role: str, plant: str | None) -> str | None:
	rows = frappe.db.sql("""select u.name from `tabUser` u join `tabHas Role` r on r.parent = u.name
		where r.role = %s and u.enabled = 1 and u.user_type = 'System User' and u.name not in ('Administrator', 'Guest')
		order by u.name""", role, pluck=True)
	for user in rows:
		user_plant = frappe.db.get_value("User Permission", {"user": user, "allow": "Plant", "is_default": 1},
			"for_value")
		if user_plant == plant:
			return user
	return rows[0] if rows else None


def _partner(party_type: str) -> str | None:
	if party_type == "Supplier":  # a just-in-time supplier: the Kanban journey needs one
		rows = frappe.db.sql("""select pua.user from `tabPartner User Access` pua
			join `tabKanban Loop` kl on kl.supplier = pua.supplier and kl.status = 'Active'
			where pua.status = 'Active' and pua.party_type = 'Supplier' order by pua.user limit 1""", pluck=True)
		if rows:
			return rows[0]
	return frappe.db.get_value("Partner User Access", {"party_type": party_type, "status": "Active"}, "user",
		order_by="user asc")


def prepare_users(password: str | None = None, output: str | None = None) -> str:
	"""Returns the credentials JSON, or writes it to ``output`` (mode 0600) and returns a
	summary without the password."""
	guard()
	frappe.only_for("System Manager")
	password = password or frappe.generate_hash(length=24)
	users: dict[str, str] = {}
	for key, (role, plant) in PERSONAS.items():
		user = _user_with(role, plant)
		if user:
			users[key] = user
	users["supplier"] = _partner("Supplier")
	users["dealer"] = _partner("Customer")
	# a second treasury approver: payments need two distinct approvers
	others = frappe.db.sql("""select u.name from `tabUser` u join `tabHas Role` r on r.parent = u.name
		where r.role = 'Treasury Approver' and u.enabled = 1 and u.name != %s order by u.name""",
		users.get("treasury") or "", pluck=True)
	if others:
		users["treasury_2"] = others[0]
	for user in users.values():
		if user:
			update_password(user, password)
	frappe.db.commit()
	payload = json.dumps({"password": password, "users": users}, sort_keys=True)
	if output:
		import os

		fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
		with os.fdopen(fd, "w") as handle:
			handle.write(payload)
		return f"{len(users)} personas written to {output}"
	return payload
