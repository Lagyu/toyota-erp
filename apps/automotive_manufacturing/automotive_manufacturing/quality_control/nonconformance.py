"""Nonconformance closure control (ERP-07): a case resolves only when containment and
root cause are recorded, every disposition is executed, the CAPA (Quality Action) is
completed, and a Quality Manager other than the reporter approves closure."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from automotive_core.governance import audit


class NonconformanceClosureError(frappe.ValidationError):
	pass


def validate(doc, method=None) -> None:
	if doc.is_new() or not doc.has_value_changed("status") or doc.status != "Resolved":
		return
	problems = []
	if not (doc.get("corrective_action") or "").strip():
		problems.append(_("corrective action (containment) is empty"))
	if not (doc.get("automotive_root_cause") or "").strip():
		problems.append(_("root cause is not recorded"))
	drafts = frappe.get_all("Quality Disposition", filters={"nonconformance": doc.name, "docstatus": 0}, pluck="name")
	if drafts:
		problems.append(_("dispositions not executed: {0}").format(", ".join(drafts)))
	if doc.get("automotive_quality_hold"):
		status = frappe.db.get_value("Quality Hold", doc.automotive_quality_hold, "status")
		if status == "Active":
			problems.append(_("quality hold {0} is still active").format(doc.automotive_quality_hold))
	if doc.get("automotive_capa"):
		if frappe.db.get_value("Quality Action", doc.automotive_capa, "status") != "Completed":
			problems.append(_("CAPA {0} is not completed").format(doc.automotive_capa))
	else:
		problems.append(_("no corrective/preventive action is linked"))
	user = frappe.session.user
	if "Quality Manager" not in frappe.get_roles(user):
		problems.append(_("closure needs a Quality Manager"))
	elif user == doc.owner and user != "Administrator":
		problems.append(_("the reporter cannot approve closure"))
	if problems:
		raise NonconformanceClosureError(_("Cannot resolve {0}: {1}.").format(doc.name, "; ".join(problems)))
	doc.automotive_closure_approved_by = user
	doc.automotive_closure_approved_at = now_datetime()
	audit.record("quality.nonconformance_closed", doc.doctype, doc.name,
		{"capa": doc.automotive_capa, "hold": doc.get("automotive_quality_hold")}, company=doc.get("automotive_company"))
