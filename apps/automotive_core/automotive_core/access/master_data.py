"""Master-data duplicate prevention (ERP-01).

Partners: blocks exact duplicates (tax id, partner code) and near-duplicate names (fuzzy
match via rapidfuzz, an ERPNext dependency) among active partners. A near-duplicate can be
accepted deliberately by setting ``doc.flags.accept_similar_name`` from a reviewed
onboarding flow, which is then recorded in the audit chain.

Items: one active item per supplier part number of a supplier, and per name within an
item group (a template's variants excepted). Exact rules only: a fuzzy comparison over
the catalogue's thousands of parts on every save would be too slow, and engineering
revisions of a part legitimately carry near-identical names.
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
			raise DuplicateMasterDataError(
				_("{0} {1} already uses tax id {2}.").format(label, clash, doc.tax_id)
			)
	code = doc.get("automotive_partner_code")
	if code:
		doc.automotive_partner_code = code.strip().upper()
		clash = frappe.db.get_value(
			doctype,
			{"automotive_partner_code": doc.automotive_partner_code, "name": ("!=", doc.name)},
			"name",
		)
		if clash:
			raise DuplicateMasterDataError(_("Partner code {0} is already used by {1}.").format(code, clash))
	if not doc.is_new() and not doc.has_value_changed(name_field):
		return
	target = normalise_name(doc.get(name_field))
	if not target:
		return
	candidates = frappe.get_all(
		doctype, filters={"disabled": 0, "name": ("!=", doc.name or "")}, fields=["name", name_field]
	)
	for row in candidates:
		score = fuzz.token_sort_ratio(target, normalise_name(row.get(name_field)))
		if score >= SIMILARITY_THRESHOLD:
			if doc.flags.get("accept_similar_name"):
				_accept_similar(doc, row.name, score)
				return
			raise DuplicateMasterDataError(
				_("{0} name is too similar to existing {1} ({2}%).").format(label, row.name, int(score))
			)


def _accept_similar(doc, similar_to: str, score: float) -> None:
	"""A reviewed near-duplicate, recorded in the audit chain once the document is saved: the
	audit event links to it, so a new document has to exist first."""
	doc.flags.automotive_similar_name = {"similar_to": similar_to, "score": score}


def record_accepted_similarity(doc, method=None) -> None:
	"""on_update of partners and items: the audit record of an accepted near-duplicate."""
	similar = doc.flags.pop("automotive_similar_name", None)
	if similar:
		audit.record("master_data.similar_name_accepted", doc.doctype, doc.name, similar)


def validate_supplier(doc, method=None) -> None:
	_check(doc, "supplier_name", _("Supplier"))


def validate_customer(doc, method=None) -> None:
	_check(doc, "customer_name", _("Customer"))


def validate_item(doc, method=None) -> None:
	if doc.disabled:
		return
	for row in doc.get("supplier_items") or []:
		if not (row.supplier and row.supplier_part_no):
			continue
		clash = frappe.db.sql(
			"""select isup.parent from `tabItem Supplier` isup join `tabItem` i on i.name = isup.parent
			where isup.supplier = %s and isup.supplier_part_no = %s and isup.parent != %s and i.disabled = 0 limit 1""",
			(row.supplier, row.supplier_part_no.strip(), doc.name or ""),
		)
		if clash:
			raise DuplicateMasterDataError(
				_("Part number {0} of supplier {1} is already item {2}.").format(
					row.supplier_part_no, row.supplier, clash[0][0]
				)
			)
	if doc.variant_of or not (
		doc.is_new() or doc.has_value_changed("item_name") or doc.has_value_changed("item_group")
	):
		return
	clash = frappe.db.get_value(
		"Item",
		{
			"item_name": (doc.item_name or "").strip(),
			"item_group": doc.item_group,
			"disabled": 0,
			"variant_of": ("is", "not set"),
			"name": ("!=", doc.name or ""),
		},
		"name",
	)
	if not clash:
		return
	if doc.flags.get("accept_similar_name"):
		_accept_similar(doc, clash, 100)
		return
	raise DuplicateMasterDataError(
		_("Item {0} in {1} already has the name {2}.").format(clash, doc.item_group, doc.item_name)
	)
