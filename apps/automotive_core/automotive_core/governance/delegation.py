"""Approval delegation with effective dates, scope and expiry (ERP-01 §2.1, ERP-10)."""

from __future__ import annotations

from dataclasses import dataclass

import frappe
from frappe import _
from frappe.utils import add_days, date_diff, flt, getdate, nowdate

from automotive_core.governance import audit
from automotive_core.utils import commit


@dataclass(frozen=True)
class DelegatedRole:
	name: str
	delegator: str
	role: str
	company: str | None
	reference_doctype: str | None
	max_amount: float

	def applies(self, row) -> bool:
		if self.company and row.get("company") and self.company != row.get("company"):
			return False
		if self.reference_doctype and self.reference_doctype != row.get("reference_doctype"):
			return False
		if self.max_amount and flt(row.get("amount")) > self.max_amount:
			return False
		return True


def _active_rows(delegate: str, on_date=None) -> list[dict]:
	day = getdate(on_date or nowdate())
	return frappe.get_all(
		"Approval Delegation",
		filters={"delegate": delegate, "docstatus": 1, "from_date": ("<=", day), "to_date": (">=", day)},
		fields=["name", "delegator", "approver_role", "scope_company", "reference_doctype", "max_amount"],
	)


def delegated_roles(delegate: str, on_date=None) -> list[DelegatedRole]:
	result = []
	for row in _active_rows(delegate, on_date):
		if not _delegator_still_holds(row.delegator, row.approver_role):
			continue
		# a role gained since the delegation was granted can make it a conflict: it then grants nothing
		if any(r.enforcement == "Block" for r in sod_conflicts(delegate, row.approver_role)):
			continue
		result.append(
			DelegatedRole(
				row.name,
				row.delegator,
				row.approver_role,
				row.scope_company,
				row.reference_doctype,
				flt(row.max_amount),
			)
		)
	return result


def sod_conflicts(delegate: str, role: str) -> list[dict]:
	"""Segregation-of-duties rules that the delegated role would break for the delegate:
	authority received by delegation counts like a role held (a Buyer must not approve
	payments as an acting Treasury Approver)."""
	from automotive_core.governance.sod import conflicts

	held = set(frappe.get_roles(delegate))
	return [
		r
		for r in conflicts(held | {role})
		if role in (r.role_a, r.role_b) and not (r.role_a in held and r.role_b in held)
	]


def _delegator_still_holds(delegator: str, role: str) -> bool:
	enabled = frappe.db.get_value("User", delegator, "enabled")
	return bool(enabled) and role in frappe.get_roles(delegator)


def find_active_delegation(
	delegate: str,
	role: str,
	delegator: str | None,
	company: str | None,
	doctype: str | None,
	amount: float,
	on_date=None,
):
	row = {"company": company, "reference_doctype": doctype, "amount": amount}
	for d in delegated_roles(delegate, on_date):
		if d.role != role or (delegator and d.delegator != delegator):
			continue
		if d.applies(row):
			return d
	return None


def compute_status(doc) -> str:
	if doc.docstatus == 0:
		return "Draft"
	if doc.docstatus == 2:
		return "Revoked"
	today = getdate(nowdate())
	if today < getdate(doc.from_date):
		return "Scheduled"
	if today > getdate(doc.to_date):
		return "Expired"
	return "Active"


def validate(doc) -> None:
	if doc.delegator == doc.delegate:
		frappe.throw(_("A user cannot delegate to themselves."))
	if getdate(doc.to_date) < getdate(doc.from_date):
		frappe.throw(_("To Date must be on or after From Date."))
	max_days = frappe.db.get_single_value("Automotive Settings", "max_delegation_days") or 90
	if date_diff(doc.to_date, doc.from_date) + 1 > max_days:
		frappe.throw(_("Delegations are limited to {0} days.").format(max_days))
	if doc.is_new() and getdate(doc.from_date) < add_days(getdate(nowdate()), -1):
		frappe.throw(_("A delegation cannot start in the past."))
	if doc.approver_role not in frappe.get_roles(doc.delegator):
		frappe.throw(
			_("{0} does not hold the role {1} and cannot delegate it.").format(
				doc.delegator, doc.approver_role
			)
		)
	if not frappe.db.get_value("User", doc.delegate, "enabled"):
		frappe.throw(_("Delegate {0} is not an active user.").format(doc.delegate))
	found = sod_conflicts(doc.delegate, doc.approver_role)
	blocking = [r for r in found if r.enforcement == "Block"]
	if blocking:
		from automotive_core.governance.sod import SegregationOfDutiesError

		raise SegregationOfDutiesError(
			_("Segregation of duties: {0} may not act as {1} ({2}).").format(
				doc.delegate, doc.approver_role, "; ".join(f"{r.role_a} + {r.role_b}" for r in blocking)
			)
		)
	for rule in found:
		frappe.msgprint(
			_("Segregation of duties warning ({0}): {1} would hold {2} and {3}.").format(
				rule.name, doc.delegate, rule.role_a, rule.role_b
			),
			indicator="orange",
			alert=True,
		)
	user = frappe.session.user
	privileged = {"System Manager", "IT Operator"} & set(frappe.get_roles(user))
	if user != doc.delegator and not privileged:
		raise frappe.PermissionError(_("Only the delegator or an IT Operator can create this delegation."))
	overlapping = frappe.get_all(
		"Approval Delegation",
		filters={
			"delegator": doc.delegator,
			"approver_role": doc.approver_role,
			"docstatus": 1,
			"name": ("!=", doc.name),
			"from_date": ("<=", doc.to_date),
			"to_date": (">=", doc.from_date),
		},
		pluck="name",
	)
	if overlapping:
		frappe.throw(
			_("{0} already delegates {1} in this period ({2}).").format(
				doc.delegator, doc.approver_role, ", ".join(overlapping)
			)
		)
	doc.status = compute_status(doc)


def on_submit(doc) -> None:
	doc.db_set("status", compute_status(doc))
	audit.record(
		"delegation.granted",
		doc.doctype,
		doc.name,
		{
			"delegator": doc.delegator,
			"delegate": doc.delegate,
			"role": doc.approver_role,
			"from": str(doc.from_date),
			"to": str(doc.to_date),
			"company": doc.scope_company,
			"doctype": doc.reference_doctype,
			"max_amount": flt(doc.max_amount),
		},
		company=doc.scope_company,
	)


def on_cancel(doc) -> None:
	doc.db_set("status", "Revoked")
	audit.record(
		"delegation.revoked",
		doc.doctype,
		doc.name,
		{"reason": doc.flags.get("cancel_reason")},
		company=doc.scope_company,
	)


def refresh_statuses() -> None:
	for row in frappe.get_all(
		"Approval Delegation",
		filters={"docstatus": 1},
		fields=["name", "status", "from_date", "to_date", "docstatus"],
	):
		status = compute_status(frappe._dict(row))
		if status != row.status:
			frappe.db.set_value("Approval Delegation", row.name, "status", status, update_modified=False)
	commit()
