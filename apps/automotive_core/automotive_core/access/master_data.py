"""Master-data duplicate prevention for partners (ERP-01).

Blocks exact duplicates (tax id, partner code) and near-duplicate names (fuzzy match
via rapidfuzz, an ERPNext dependency) among active partners. A near-duplicate can be
accepted deliberately by setting ``doc.flags.accept_similar_name`` from a reviewed
onboarding flow, which is then recorded in the audit chain.
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from rapidfuzz import fuzz

from automotive_core.governance import audit

SIMILARITY_THRESHOLD = 92
LEGAL_SUFFIXES = re.compile(
	r"\b(co|corp|corporation|inc|ltd|llc|limited|kk|k\.k\.|gk|plc|gmbh|sa|company)\b\.?", re.IGNORECASE
)


class DuplicateMasterDataError(frappe.ValidationError):
	pass


def normalise_name(name: str) -> str:
	name = (name or "").lower().replace("株式会社", " ").replace("合同会社", " ")
	name = LEGAL_SUFFIXES.sub(" ", name)
	return " ".join(re.sub(r"[^\w\s]", " ", name).split())


def _check(doc, name_field: str, label: str) -> None:
	doctype = doc.doctype
	if doc.get("tax_id"):
		clash = frappe.db.get_value(
			doctype, {"tax_id": doc.tax_id, "name": ("!=", doc.name), "disabled": 0}, "name"
		)
		if clash:
			raise DuplicateMasterDataError(_("{0} {1} already uses tax id {2}.").format(label, clash, doc.tax_id))
	code = doc.get("automotive_partner_code")
	if code:
		doc.automotive_partner_code = code.strip().upper()
		clash = frappe.db.get_value(
			doctype, {"automotive_partner_code": doc.automotive_partner_code, "name": ("!=", doc.name)}, "name"
		)
		if clash:
			raise DuplicateMasterDataError(_("Partner code {0} is already used by {1}.").format(code, clash))
	if not doc.is_new() and not doc.has_value_changed(name_field):
		return
	target = normalise_name(doc.get(name_field))
	if not target:
		return
	candidates = frappe.get_all(doctype, filters={"disabled": 0, "name": ("!=", doc.name or "")}, fields=["name", name_field])
	for row in candidates:
		score = fuzz.token_sort_ratio(target, normalise_name(row.get(name_field)))
		if score >= SIMILARITY_THRESHOLD:
			if doc.flags.get("accept_similar_name"):
				audit.record("master_data.similar_name_accepted", doctype, doc.name,
					{"similar_to": row.name, "score": score})
				return
			raise DuplicateMasterDataError(
				_("{0} name is too similar to existing {1} ({2}%).").format(label, row.name, int(score))
			)


def validate_supplier(doc, method=None) -> None:
	_check(doc, "supplier_name", _("Supplier"))


def validate_customer(doc, method=None) -> None:
	_check(doc, "customer_name", _("Customer"))
