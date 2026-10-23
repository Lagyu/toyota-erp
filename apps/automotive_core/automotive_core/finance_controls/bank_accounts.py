"""Restricted supplier bank details and payment-release guard (ERP-09).

* Account numbers are permlevel 1 (setup/permissions.py): only finance roles see them.
* Any change of a party bank account's identifying fields resets it to *Pending
  Verification*; a Treasury Approver other than the person who made the change must
  verify it (maker/checker).
* A supplier payment by bank transfer can only use a *Verified* account.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from automotive_core.governance import audit
from automotive_core.utils import canonical_json, sha256_hex

IDENTIFYING_FIELDS = ("bank", "bank_account_no", "iban", "branch_code", "party_type", "party")


class UnverifiedBankAccountError(frappe.ValidationError):
	pass


def fingerprint(doc) -> str:
	return sha256_hex(canonical_json({f: (doc.get(f) or "").strip() if isinstance(doc.get(f), str) else doc.get(f)
		for f in IDENTIFYING_FIELDS}))


def validate_bank_account(doc, method=None) -> None:
	new_fp = fingerprint(doc)
	if doc.automotive_account_fingerprint == new_fp:
		return
	changed_from = doc.automotive_account_fingerprint
	doc.automotive_account_fingerprint = new_fp
	if doc.party_type:
		doc.automotive_verification_status = "Pending Verification"
		doc.automotive_verified_by = None
		doc.automotive_verified_at = None
		if not doc.is_new():
			audit.record("bank_account.changed", doc.doctype, doc.name,
				{"party_type": doc.party_type, "party": doc.party, "previous_fingerprint": changed_from,
				 "fingerprint": new_fp})


@frappe.whitelist(methods=["POST"])
def verify(bank_account: str, comment: str | None = None) -> dict:
	frappe.only_for(("Treasury Approver",))
	doc = frappe.get_doc("Bank Account", bank_account)
	if doc.automotive_verification_status == "Verified":
		return {"status": "Verified"}
	user = frappe.session.user
	last_change = frappe.get_all(
		"Version", filters={"ref_doctype": "Bank Account", "docname": doc.name}, fields=["owner"],
		order_by="creation desc", limit=1,
	)
	makers = {doc.owner, doc.modified_by} | {v.owner for v in last_change}
	if user in makers:
		raise frappe.PermissionError(_("The person who entered or changed the account cannot verify it."))
	doc.db_set({
		"automotive_verification_status": "Verified",
		"automotive_verified_by": user,
		"automotive_verified_at": now_datetime(),
	})
	audit.record("bank_account.verified", doc.doctype, doc.name,
		{"fingerprint": doc.automotive_account_fingerprint, "comment": comment})
	return {"status": "Verified"}


def _is_bank_transfer(doc) -> bool:
	if not doc.mode_of_payment:
		return bool(doc.party_bank_account)
	return frappe.get_cached_value("Mode of Payment", doc.mode_of_payment, "type") == "Bank"


def validate_payment_entry(doc, method=None) -> None:
	if doc.payment_type != "Pay" or doc.party_type != "Supplier" or not _is_bank_transfer(doc):
		return
	if not doc.party_bank_account:
		raise UnverifiedBankAccountError(_("Bank transfers to suppliers need the supplier's verified bank account."))
	status = frappe.db.get_value("Bank Account", doc.party_bank_account, "automotive_verification_status")
	if status != "Verified":
		raise UnverifiedBankAccountError(
			_("Bank account {0} is {1}; a Treasury Approver must verify it before payment.").format(
				doc.party_bank_account, _(status or "Unverified")
			)
		)
