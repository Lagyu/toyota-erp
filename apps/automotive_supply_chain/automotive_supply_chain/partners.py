"""Partner (supplier / dealer) scoping for portal users, enforced on the server."""

from __future__ import annotations

import frappe
from frappe import _


def party_of_user(user: str | None = None) -> tuple[str, str] | None:
	user = user or frappe.session.user
	row = frappe.db.get_value("Partner User Access", {"user": user, "status": "Active"},
		["party_type", "supplier", "customer"], as_dict=True)
	if not row:
		return None
	return row.party_type, (row.supplier if row.party_type == "Supplier" else row.customer)


def is_portal_user(user: str | None = None) -> bool:
	user = user or frappe.session.user
	return frappe.db.get_value("User", user, "user_type") == "Website User"


def assert_party_access(party_type: str, party: str) -> None:
	"""Portal users may only act for the one partner they belong to; desk users pass
	(their DocType permissions and User Permissions apply)."""
	if frappe.session.user in ("Administrator",) or not is_portal_user():
		return
	mine = party_of_user()
	if mine != (party_type, party):
		raise frappe.PermissionError(_("You can only act for your own organisation."))


def require_party(party_type: str) -> str:
	mine = party_of_user()
	if not mine or mine[0] != party_type:
		raise frappe.PermissionError(_("This portal is for {0} users.").format(_(party_type).lower()))
	return mine[1]
