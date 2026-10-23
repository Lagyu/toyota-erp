"""Intercompany transfer pricing and AR/AP balancing (ERP-09, J-07).

Orders between group companies (ERPNext internal suppliers/customers) must carry the
price of the submitted Transfer Price Agreement valid on the order date. ERPNext's
own inter-company mappers (PO -> SO, DN -> PR, SI -> PI) create the mirrored
documents; ``reconciliation`` proves both companies booked matching amounts.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate


def agreement(seller: str, buyer: str, item_code: str, on_date) -> dict | None:
	day = getdate(on_date)
	group = frappe.get_cached_value("Item", item_code, "item_group")
	rows = frappe.db.sql(
		"""select name, pricing_method, price, markup_percent, currency from `tabTransfer Price Agreement`
		where docstatus = 1 and seller_company = %(seller)s and buyer_company = %(buyer)s
		and (item_code = %(item)s or (ifnull(item_code, '') = '' and item_group = %(group)s))
		and valid_from <= %(day)s and ifnull(valid_to, '9999-12-31') >= %(day)s
		order by (item_code = %(item)s) desc, valid_from desc limit 1""",
		{"seller": seller, "buyer": buyer, "item": item_code, "group": group, "day": day},
		as_dict=True,
	)
	return rows[0] if rows else None


def agreed_price(seller: str, buyer: str, item_code: str, on_date) -> tuple[float, str, str] | None:
	tpa = agreement(seller, buyer, item_code, on_date)
	if not tpa:
		return None
	if tpa.pricing_method == "Fixed Price":
		return flt(tpa.price), tpa.currency, tpa.name
	cost = flt(frappe.get_cached_value("Item", item_code, "automotive_standard_cost")) or flt(
		frappe.get_cached_value("Item", item_code, "valuation_rate"))
	return flt(cost * (1 + flt(tpa.markup_percent) / 100), 2), tpa.currency, tpa.name


def _check(doc, seller: str, buyer: str, date_field: str) -> None:
	for row in doc.items:
		price = agreed_price(seller, buyer, row.item_code, doc.get(date_field))
		if not price:
			frappe.throw(_("Row {0}: no transfer price agreement {1} -> {2} for {3}.").format(
				row.idx, seller, buyer, row.item_code))
		rate, currency, name = price
		if currency != doc.currency:
			frappe.throw(_("Row {0}: agreement {1} is in {2}, the order in {3}.").format(row.idx, name, currency,
				doc.currency))
		if abs(flt(row.rate) - rate) > 0.005:
			frappe.throw(_("Row {0}: intercompany rate {1} differs from transfer price {2} ({3}).").format(
				row.idx, row.rate, rate, name))


def validate_purchase_order(doc, method=None) -> None:
	internal = frappe.db.get_value("Supplier", doc.supplier, ["is_internal_supplier", "represents_company"], as_dict=True)
	if internal and internal.is_internal_supplier and doc.docstatus < 2:
		_check(doc, internal.represents_company, doc.company, "transaction_date")


def validate_sales_order(doc, method=None) -> None:
	internal = frappe.db.get_value("Customer", doc.customer, ["is_internal_customer", "represents_company"], as_dict=True)
	if internal and internal.is_internal_customer and doc.docstatus < 2:
		_check(doc, doc.company, internal.represents_company, "transaction_date")


def validate_agreement(doc) -> None:
	if doc.seller_company == doc.buyer_company:
		frappe.throw(_("Seller and buyer must be different companies."))
	if bool(doc.item_code) == bool(doc.item_group):
		frappe.throw(_("Give either an item or an item group."))
	if doc.pricing_method == "Fixed Price" and flt(doc.price) <= 0:
		frappe.throw(_("A fixed transfer price must be positive."))


@frappe.whitelist()
def reconciliation(seller: str, buyer: str, to_date: str | None = None) -> dict:
	"""Seller's receivable from the buyer vs the buyer's payable to the seller (invoice level)."""
	frappe.only_for(("Group Controller", "Accounts Manager", "Auditor", "System Manager"))
	to_date = to_date or frappe.utils.nowdate()
	customer = frappe.db.get_value("Customer", {"is_internal_customer": 1, "represents_company": buyer}, "name")
	supplier = frappe.db.get_value("Supplier", {"is_internal_supplier": 1, "represents_company": seller}, "name")
	sales = frappe.get_all("Sales Invoice", filters={"company": seller, "customer": customer, "docstatus": 1,
		"posting_date": ("<=", to_date)}, fields=["name", "grand_total", "outstanding_amount", "currency",
		"inter_company_invoice_reference"])
	purchases = frappe.get_all("Purchase Invoice", filters={"company": buyer, "supplier": supplier, "docstatus": 1,
		"posting_date": ("<=", to_date)}, fields=["name", "grand_total", "outstanding_amount", "currency",
		"inter_company_invoice_reference"])
	by_ref = {p.inter_company_invoice_reference: p for p in purchases if p.inter_company_invoice_reference}
	unmatched_sales = [s.name for s in sales if s.name not in by_ref]
	mismatched = [(s.name, by_ref[s.name].name, flt(s.grand_total), flt(by_ref[s.name].grand_total))
		for s in sales if s.name in by_ref and abs(flt(s.grand_total) - flt(by_ref[s.name].grand_total)) > 0.01]
	matched_ids = {s.name for s in sales}
	unmatched_purchases = [p.name for p in purchases if p.inter_company_invoice_reference not in matched_ids]
	ar = sum(flt(s.outstanding_amount) for s in sales)
	ap = sum(flt(p.outstanding_amount) for p in purchases)
	# Outstanding amounts are in each company's party-account currency; compare them only
	# when both sides book in the same currency (cross-currency pairs differ by FX).
	seller_currency = frappe.get_cached_value("Company", seller, "default_currency")
	buyer_currency = frappe.get_cached_value("Company", buyer, "default_currency")
	outstanding_comparable = seller_currency == buyer_currency
	return {
		"seller": seller, "buyer": buyer, "to_date": str(to_date),
		"receivable_outstanding": ar, "payable_outstanding": ap,
		"invoiced_by_seller": sum(flt(s.grand_total) for s in sales),
		"booked_by_buyer": sum(flt(p.grand_total) for p in purchases),
		"unmatched_sales_invoices": unmatched_sales, "unmatched_purchase_invoices": unmatched_purchases,
		"amount_mismatches": mismatched,
		"outstanding_comparable": outstanding_comparable,
		"balanced": not unmatched_sales and not unmatched_purchases and not mismatched
		and (abs(ar - ap) <= 0.01 or not outstanding_comparable),
	}
