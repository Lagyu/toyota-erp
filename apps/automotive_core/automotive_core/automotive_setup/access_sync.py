"""Effective-dated access from Plant Assignments and Partner User Access (ERP-01, ERP-10).

* Active Plant Assignment (grant_plant_access) -> managed User Permissions on Plant
  and its Company for the employee's user; Employee.automotive_plant follows the most
  recent active assignment.
* Active Partner User Access -> Contact linked to the supplier/dealer, portal roles,
  and a managed User Permission on that Supplier/Customer, so the partner user's API
  and portal reads are restricted to their own organisation.

Statuses are recomputed on save and hourly, so future-dated grants activate and
ended grants are removed without manual work.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate, nowdate

from automotive_core.access import user_permissions
from automotive_core.governance import audit
from automotive_core.utils import commit

PLANT_SOURCE = "Plant Assignment"
PARTNER_SOURCE = "Partner User Access"
PORTAL_ROLES = {
	"Supplier": ("Supplier Portal User",),
	"Customer": ("Dealer Portal User",),
}
# ERPNext's generic portal roles are not granted: its standard portal pages resolve the
# party through Supplier/Customer "Portal Users" rows instead of Partner User Access, so
# partners use the automotive portals (and their party-scoped APIs) exclusively.
UNGRANTED_ROLES = ("Supplier", "Customer")


def effective_status(from_date, to_date, revoked: bool = False) -> str:
	if revoked:
		return "Revoked"
	today = getdate(nowdate())
	if getdate(from_date) > today:
		return "Scheduled"
	if to_date and getdate(to_date) < today:
		return "Ended"
	return "Active"


# Plant assignments -------------------------------------------------------------------


def sync_plant_access(user: str | None) -> None:
	if not user:
		return
	rows = frappe.get_all(
		"Plant Assignment",
		filters={"user": user, "status": "Active", "grant_plant_access": 1},
		fields=["plant", "company"],
		order_by="from_date desc",
	)
	desired: dict[tuple[str, str], bool] = {}
	for i, row in enumerate(rows):
		desired[("Plant", row.plant)] = i == 0
		if row.company:
			desired.setdefault(("Company", row.company), i == 0)
	user_permissions.sync(user, PLANT_SOURCE, desired)


def on_plant_assignment_change(doc) -> None:
	sync_plant_access(doc.user)
	employee_plant = frappe.get_all(
		"Plant Assignment",
		filters={"employee": doc.employee, "status": "Active"},
		pluck="plant",
		order_by="from_date desc",
		limit=1,
	)
	frappe.db.set_value("Employee", doc.employee, "automotive_plant", employee_plant[0] if employee_plant else None,
		update_modified=False)


# Partner access ----------------------------------------------------------------------


def party_of(doc) -> tuple[str, str]:
	return (doc.party_type, doc.supplier if doc.party_type == "Supplier" else doc.customer)


def sync_partner_access(user: str) -> None:
	rows = frappe.get_all(
		"Partner User Access",
		filters={"user": user, "status": "Active"},
		fields=["party_type", "supplier", "customer"],
	)
	desired: dict[tuple[str, str], bool] = {}
	needed_roles: set[str] = set()
	for row in rows:
		party_type, party = party_of(row)
		desired[(party_type, party)] = not desired
		needed_roles.update(PORTAL_ROLES[party_type])
	user_permissions.sync(user, PARTNER_SOURCE, desired)

	user_doc = frappe.get_doc("User", user)
	all_portal_roles = {r for pair in PORTAL_ROLES.values() for r in pair} | set(UNGRANTED_ROLES)
	current = {r.role for r in user_doc.roles}
	wanted = (current - all_portal_roles) | needed_roles
	if wanted != current:
		user_doc.set("roles", [])
		user_doc.append_roles(*sorted(wanted))
		user_doc.flags.ignore_permissions = True
		user_doc.save()


def strip_ungranted_roles(doc, method=None) -> None:
	"""User.on_update (after ERPNext's): ERPNext gives every user whose Contact links a
	Supplier/Customer its generic portal role on each save (erpnext.portal.utils.
	set_default_role); partner users keep only the automotive portal roles."""
	if doc.user_type != "Website User" or not frappe.db.exists("Partner User Access", {"user": doc.name}):
		return
	extra = sorted({r.role for r in doc.roles if r.role in UNGRANTED_ROLES})
	if not extra:
		return
	frappe.db.delete("Has Role", {"parenttype": "User", "parent": doc.name, "role": ("in", extra)})
	doc.set("roles", [r for r in doc.roles if r.role not in UNGRANTED_ROLES])
	frappe.clear_cache(user=doc.name)


def ensure_contact(doc) -> str:
	party_type, party = party_of(doc)
	contact_name = frappe.db.get_value("Contact", {"user": doc.user}, "name")
	if contact_name:
		contact = frappe.get_doc("Contact", contact_name)
	else:
		user = frappe.db.get_value("User", doc.user, ["first_name", "last_name", "email"], as_dict=True)
		contact = frappe.new_doc("Contact")
		contact.first_name = user.first_name or doc.user
		contact.last_name = user.last_name
		contact.user = doc.user
		contact.append("email_ids", {"email_id": user.email, "is_primary": 1})
	if not any(link.link_doctype == party_type and link.link_name == party for link in contact.links):
		contact.append("links", {"link_doctype": party_type, "link_name": party})
	contact.flags.ignore_permissions = True
	contact.save()
	return contact.name


def remove_contact_link(doc) -> None:
	party_type, party = party_of(doc)
	contact_name = doc.contact or frappe.db.get_value("Contact", {"user": doc.user}, "name")
	if not contact_name:
		return
	still_active = frappe.db.exists(
		"Partner User Access",
		{"user": doc.user, "status": "Active", "name": ("!=", doc.name), "party_type": party_type,
		 "supplier" if party_type == "Supplier" else "customer": party},
	)
	if still_active:
		return
	contact = frappe.get_doc("Contact", contact_name)
	contact.links = [lnk for lnk in contact.links if not (lnk.link_doctype == party_type and lnk.link_name == party)]
	contact.flags.ignore_permissions = True
	contact.save()


def on_partner_access_change(doc) -> None:
	if doc.status == "Active":
		contact = ensure_contact(doc)
		if doc.contact != contact:
			doc.db_set("contact", contact, update_modified=False)
	else:
		remove_contact_link(doc)
	sync_partner_access(doc.user)


def validate_partner_access(doc) -> None:
	if frappe.db.get_value("User", doc.user, "user_type") != "Website User":
		frappe.throw(_("Partner access is for portal (Website) users; {0} is a desk user.").format(doc.user))
	party_type, party = party_of(doc)
	if not party:
		frappe.throw(_("Select the {0}.").format(_(party_type)))
	other_party = frappe.get_all(
		"Partner User Access",
		filters={"user": doc.user, "status": ("in", ("Active", "Scheduled")), "name": ("!=", doc.name)},
		fields=["party_type", "supplier", "customer"],
	)
	for row in other_party:
		if party_of(row) != (party_type, party):
			frappe.throw(_("{0} already represents {1}; a portal user belongs to exactly one partner.").format(
				doc.user, party_of(row)[1]))
	if doc.to_date and getdate(doc.to_date) < getdate(doc.from_date):
		frappe.throw(_("To Date must be on or after From Date."))


# Scheduler ---------------------------------------------------------------------------


def refresh_assignments() -> None:
	for row in frappe.get_all("Plant Assignment", fields=["name", "status", "from_date", "to_date", "user"]):
		status = effective_status(row.from_date, row.to_date)
		if status != row.status:
			doc = frappe.get_doc("Plant Assignment", row.name)
			doc.db_set("status", status, update_modified=False)
			on_plant_assignment_change(doc)
			audit.record("access.plant_assignment." + status.lower(), "Plant Assignment", row.name)
	for row in frappe.get_all("Partner User Access", filters={"status": ("!=", "Revoked")},
			fields=["name", "status", "from_date", "to_date"]):
		status = effective_status(row.from_date, row.to_date)
		if status != row.status:
			doc = frappe.get_doc("Partner User Access", row.name)
			doc.db_set("status", status, update_modified=False)
			on_partner_access_change(doc)
			audit.record("access.partner." + status.lower(), "Partner User Access", row.name)
	commit()
