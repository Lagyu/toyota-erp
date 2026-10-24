"""Supplier onboarding, approved supplier list and sourcing decisions (ERP-05)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from automotive_core.finance_controls.taxes import default_tax_category
from automotive_core.governance import audit
from automotive_core.utils import send_welcome_email

# ---------------------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------------------


def onboarding_company(doc) -> str | None:
	"""The company a new supplier is taxed against: that of the first plant it will supply."""
	for row in doc.get("proposed_items") or []:
		if row.plant:
			return frappe.get_cached_value("Plant", row.plant, "company")
	return frappe.db.get_single_value("Global Defaults", "default_company")


def validate_onboarding(doc) -> None:
	doc.partner_code = (doc.partner_code or "").strip().upper()
	if not doc.tax_category:
		doc.tax_category = default_tax_category("Supplier", onboarding_company(doc), doc.country)
	if frappe.db.exists("Supplier", {"tax_id": doc.tax_id, "disabled": 0}):
		frappe.throw(_("A supplier with tax id {0} already exists.").format(doc.tax_id))
	today = getdate(nowdate())
	for cert in doc.certifications:
		if getdate(cert.valid_until) < today:
			frappe.throw(_("Certificate {0} ({1}) has expired.").format(cert.certificate_no, cert.standard))
	if not any(c.standard in ("IATF 16949", "ISO 9001") for c in doc.certifications):
		frappe.throw(_("Production suppliers need an IATF 16949 or ISO 9001 certificate."))
	emails = [c.email.lower() for c in doc.contacts]
	if len(emails) != len(set(emails)):
		frappe.throw(_("A contact email appears twice."))
	if doc.docstatus == 0:
		doc.status = "Draft"


def on_onboarding_submit(doc) -> None:
	from automotive_core.automotive_setup.access_sync import effective_status

	supplier = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": doc.supplier_name,
			"supplier_group": doc.supplier_group,
			"country": doc.country,
			"tax_id": doc.tax_id,
			"tax_category": doc.tax_category,
			"automotive_partner_code": doc.partner_code,
			"automotive_onboarding": doc.name,
			"automotive_risk_rating": doc.risk_rating,
		}
	)
	supplier.flags.ignore_permissions = True
	supplier.insert()
	for item in doc.proposed_items:
		frappe.get_doc(
			{
				"doctype": "Approved Supplier List",
				"supplier": supplier.name,
				"item_code": item.item_code,
				"item_group": item.item_group,
				"plant": item.plant,
				"status": "Provisional",
				"share_percent": 0,
				"valid_from": nowdate(),
				"supplier_onboarding": doc.name,
			}
		).insert(ignore_permissions=True)
	for contact in doc.contacts:
		if not contact.portal_access:
			continue
		if not frappe.db.exists("User", contact.email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": contact.email,
					"first_name": contact.first_name,
					"last_name": contact.last_name,
					"user_type": "Website User",
					"send_welcome_email": send_welcome_email(),
				}
			).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Partner User Access",
				"user": contact.email,
				"party_type": "Supplier",
				"supplier": supplier.name,
				"partner_role": contact.partner_role,
				"from_date": nowdate(),
			}
		).insert(ignore_permissions=True)
	doc.db_set({"status": "Approved", "supplier": supplier.name})
	audit.record(
		"supplier.onboarded", "Supplier", supplier.name, {"onboarding": doc.name, "risk": doc.risk_rating}
	)


# ---------------------------------------------------------------------------------------
# Approved supplier list
# ---------------------------------------------------------------------------------------


def validate_asl(doc) -> None:
	if bool(doc.item_code) == bool(doc.item_group):
		frappe.throw(_("Give either an item or an item group."))
	if doc.valid_to and getdate(doc.valid_to) < getdate(doc.valid_from):
		frappe.throw(_("Valid To is before Valid From."))
	if doc.status == "Approved" and doc.ppap_status == "Rejected":
		frappe.throw(_("A rejected PPAP cannot be approved for supply."))
	filters = {
		"supplier": doc.supplier,
		"plant": doc.plant,
		"name": ("!=", doc.name),
		"status": ("in", ("Provisional", "Approved")),
	}
	filters.update({"item_code": doc.item_code} if doc.item_code else {"item_group": doc.item_group})
	if frappe.db.exists("Approved Supplier List", filters):
		frappe.throw(_("{0} already has an active entry for this item/plant.").format(doc.supplier))


def approved_entries(item_code: str, plant: str | None, on_date=None) -> list[dict]:
	day = getdate(on_date or nowdate())
	group = frappe.get_cached_value("Item", item_code, "item_group")
	rows = frappe.db.sql(
		"""select name, supplier, share_percent, plant from `tabApproved Supplier List`
		where status = 'Approved' and (item_code = %(item)s or item_group = %(group)s)
		and (ifnull(plant, '') = '' or plant = %(plant)s)
		and valid_from <= %(day)s and ifnull(valid_to, '9999-12-31') >= %(day)s
		order by share_percent desc, supplier asc""",
		{"item": item_code, "group": group, "plant": plant or "", "day": day},
		as_dict=True,
	)
	return rows


def preferred_supplier(item_code: str, company: str) -> str | None:
	"""Planning supplier resolver hook: the approved source with the largest share."""
	plants = frappe.get_all("Plant", filters={"company": company}, pluck="name")
	for plant in plants or [None]:
		rows = approved_entries(item_code, plant)
		if rows:
			return rows[0].supplier
	return None


def asl_enforced(item_code: str) -> bool:
	groups = {r.item_group for r in frappe.get_cached_doc("Supply Chain Settings").asl_item_groups or []}
	return bool(groups) and frappe.get_cached_value("Item", item_code, "item_group") in groups


def validate_purchase_order_sources(doc, method=None) -> None:
	if doc.docstatus == 2 or frappe.db.get_value("Supplier", doc.supplier, "is_internal_supplier"):
		return
	plant = doc.get("automotive_plant")
	for row in doc.items:
		if not asl_enforced(row.item_code):
			continue
		suppliers = {r.supplier for r in approved_entries(row.item_code, plant, doc.transaction_date)}
		if doc.supplier not in suppliers:
			frappe.throw(
				_("Row {0}: {1} is not an approved source of {2} for plant {3}.").format(
					row.idx, doc.supplier, row.item_code, plant or "-"
				)
			)


# ---------------------------------------------------------------------------------------
# Sourcing decision
# ---------------------------------------------------------------------------------------


def score(doc) -> None:
	quotes = doc.quotes
	if not quotes:
		frappe.throw(_("Add at least one quotation."))
	prices = [flt(q.unit_price) for q in quotes]
	leads = [int(q.lead_time_days) for q in quotes]
	best_price, best_lead = min(prices), min(leads)
	weights = (flt(doc.price_weight), flt(doc.lead_time_weight), flt(doc.quality_weight))
	if doc.method == "Best Value" and round(sum(weights), 6) != 100:
		frappe.throw(_("Weights must add up to 100%."))
	for q in quotes:
		if doc.method == "Lowest Price":
			q.total_score = round(best_price / flt(q.unit_price) * 100, 2)
		else:
			price_score = best_price / flt(q.unit_price) * 100
			lead_score = (best_lead / q.lead_time_days * 100) if q.lead_time_days else 100
			q.total_score = round(
				(price_score * weights[0] + lead_score * weights[1] + flt(q.quality_score) * weights[2])
				/ 100,
				2,
			)
	if not any(flt(q.awarded_share_percent) for q in quotes):
		max(quotes, key=lambda q: (q.total_score, -flt(q.unit_price), q.supplier)).awarded_share_percent = 100
	total_share = sum(flt(q.awarded_share_percent) for q in quotes)
	if round(total_share, 6) != 100:
		frappe.throw(_("Awarded shares must add up to 100% (now {0}%).").format(total_share))
	doc.awarded_supplier = max(quotes, key=lambda q: (flt(q.awarded_share_percent), q.total_score)).supplier


def on_sourcing_submit(doc) -> None:
	price_list = frappe.db.get_value("Price List", {"buying": 1, "enabled": 1}, "name")
	for q in doc.quotes:
		share = flt(q.awarded_share_percent)
		existing = frappe.db.get_value(
			"Approved Supplier List",
			{
				"supplier": q.supplier,
				"item_code": doc.item_code,
				"plant": doc.plant,
				"status": ("in", ("Provisional", "Approved")),
			},
			"name",
		)
		if share <= 0:
			if existing:
				frappe.db.set_value(
					"Approved Supplier List", existing, {"share_percent": 0, "sourcing_decision": doc.name}
				)
			continue
		if existing:
			asl = frappe.get_doc("Approved Supplier List", existing)
		else:
			asl = frappe.new_doc("Approved Supplier List")
			asl.update(
				{
					"supplier": q.supplier,
					"item_code": doc.item_code,
					"plant": doc.plant,
					"valid_from": doc.effective_from,
				}
			)
		asl.update({"status": "Approved", "share_percent": share, "sourcing_decision": doc.name})
		asl.flags.ignore_permissions = True
		asl.save()
		if price_list:
			price = frappe.db.get_value(
				"Item Price",
				{"item_code": doc.item_code, "price_list": price_list, "supplier": q.supplier},
				"name",
			)
			if price:
				frappe.db.set_value(
					"Item Price", price, {"price_list_rate": q.unit_price, "valid_from": doc.effective_from}
				)
			else:
				frappe.get_doc(
					{
						"doctype": "Item Price",
						"item_code": doc.item_code,
						"price_list": price_list,
						"supplier": q.supplier,
						"price_list_rate": q.unit_price,
						"valid_from": doc.effective_from,
					}
				).insert(ignore_permissions=True)
		if not frappe.db.exists("Item Supplier", {"parent": doc.item_code, "supplier": q.supplier}):
			item = frappe.get_doc("Item", doc.item_code)
			item.append("supplier_items", {"supplier": q.supplier})
			item.flags.ignore_permissions = True
			item.save()
	audit.record(
		"sourcing.awarded",
		doc.doctype,
		doc.name,
		{
			"item": doc.item_code,
			"plant": doc.plant,
			"shares": {q.supplier: flt(q.awarded_share_percent) for q in doc.quotes},
		},
		company=doc.company,
	)
