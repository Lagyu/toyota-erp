"""Cancellation with a recorded reason for governed documents (ERP-07, ERP-09).

Cancelling a DocType listed in the ``automotive_cancel_reason_doctypes`` hook is only
possible through ``cancel_with_reason``; the reason goes into the audit chain and a
timeline comment, and the amended successor keeps ``amended_from`` (ERPNext native),
so every correction leaves a trail.
"""

from __future__ import annotations

import frappe
from frappe import _

from automotive_core.utils import hook_set

MIN_REASON_LENGTH = 10


class CancelReasonRequiredError(frappe.ValidationError):
	pass


def requires_reason(doctype: str) -> bool:
	if doctype not in hook_set("automotive_cancel_reason_doctypes"):
		return False
	return bool(frappe.db.get_single_value("Automotive Settings", "require_cancel_reason"))


def guard_cancel(doc, method=None) -> None:
	if not requires_reason(doc.doctype):
		return
	reason = doc.flags.get("cancel_reason") or frappe.flags.get("automotive_cancel_reason")
	if not reason:
		raise CancelReasonRequiredError(
			_("Cancelling {0} {1} requires a reason (use Cancel With Reason).").format(_(doc.doctype), doc.name)
		)
	doc.flags.cancel_reason = reason


@frappe.whitelist(methods=["POST"])
def cancel_with_reason(doctype: str, name: str, reason: str) -> dict:
	reason = (reason or "").strip()
	if len(reason) < MIN_REASON_LENGTH:
		frappe.throw(_("Give a reason of at least {0} characters.").format(MIN_REASON_LENGTH))
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("cancel")
	if doc.docstatus != 1:
		frappe.throw(_("Only submitted documents can be cancelled."))
	doc.flags.cancel_reason = reason
	doc.cancel()
	doc.add_comment("Info", _("Cancelled with reason: {0}").format(reason))
	return {"name": doc.name, "docstatus": doc.docstatus}
