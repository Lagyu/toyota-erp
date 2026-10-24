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


# What each portal role may do (Partner User Access.partner_role), per party type. Overview and
# notices are open to every role; everything else is a business function of some roles only.
_ALL = frozenset({"Administrator", "Sales", "Logistics", "Quality", "Finance", "Viewer"})
CAPABILITIES: dict[str, dict[str, frozenset[str]]] = {
	"Supplier": {
		"quote": frozenset({"Administrator", "Sales"}),
		"orders": frozenset({"Administrator", "Sales", "Logistics", "Viewer"}),
		"acknowledge": frozenset({"Administrator", "Sales"}),
		"schedules": frozenset({"Administrator", "Sales", "Logistics", "Viewer"}),
		"confirm_schedule": frozenset({"Administrator", "Sales", "Logistics"}),
		"shipments": frozenset({"Administrator", "Sales", "Logistics", "Viewer"}),
		"ship": frozenset({"Administrator", "Logistics"}),
		"receipts": frozenset({"Administrator", "Logistics", "Quality", "Viewer"}),
		"payments": frozenset({"Administrator", "Finance"}),
		"notices": _ALL,
	},
	"Customer": {
		"catalogue": frozenset({"Administrator", "Sales", "Viewer"}),
		"order": frozenset({"Administrator", "Sales"}),
		"orders": frozenset({"Administrator", "Sales", "Logistics", "Finance", "Viewer"}),
		"inventory": frozenset({"Administrator", "Sales", "Logistics", "Viewer"}),
		"deliveries": frozenset({"Administrator", "Sales", "Logistics", "Viewer"}),
		"accept_delivery": frozenset({"Administrator", "Sales", "Logistics"}),
		"invoices": frozenset({"Administrator", "Finance"}),
		"support": _ALL,
		"raise_support": frozenset({"Administrator", "Sales", "Logistics"}),
	},
}


def portal_role(user: str | None = None) -> str | None:
	return frappe.db.get_value("Partner User Access", {"user": user or frappe.session.user, "status": "Active"},
		"partner_role")


def capabilities(party_type: str, user: str | None = None) -> list[str]:
	"""The portal functions the user's role allows (no role recorded: Viewer)."""
	role = portal_role(user) or "Viewer"
	return sorted(k for k, roles in CAPABILITIES[party_type].items() if role in roles)


def require_capability(party_type: str, capability: str) -> str:
	"""The caller's own party, if their portal role allows ``capability`` (ERP-10.4)."""
	party = require_party(party_type)
	if capability not in capabilities(party_type):
		raise frappe.PermissionError(_("Your portal role ({0}) does not allow this.").format(
			_(portal_role() or "Viewer")))
	return party
