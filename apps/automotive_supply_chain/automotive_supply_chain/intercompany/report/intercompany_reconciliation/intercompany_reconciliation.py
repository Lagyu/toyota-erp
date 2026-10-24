# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Intercompany reconciliation (ERP-09): for every pair of group companies that trade,
the seller's invoices against the buyer's bookings, with unmatched and mismatched
documents listed under the pair."""

import frappe
from frappe import _
from frappe.utils import nowdate

from automotive_core.analytics import scope
from automotive_supply_chain.intercompany.transfer_pricing import reconciliation


def _pairs(companies: set[str]) -> list[tuple[str, str]]:
	rows = frappe.db.sql("""select distinct si.company seller, c.represents_company buyer
		from `tabSales Invoice` si join `tabCustomer` c on c.name = si.customer
		where si.docstatus = 1 and c.is_internal_customer = 1 and ifnull(c.represents_company, '') != ''""")
	return sorted((s, b) for s, b in rows if s in companies or b in companies)


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = [
		{"fieldname": "label", "label": _("Pair / Document"), "fieldtype": "Data", "width": 300},
		{"fieldname": "invoiced_by_seller", "label": _("Invoiced by Seller"), "fieldtype": "Float", "width": 150},
		{"fieldname": "booked_by_buyer", "label": _("Booked by Buyer"), "fieldtype": "Float", "width": 150},
		{"fieldname": "receivable_outstanding", "label": _("Seller Receivable"), "fieldtype": "Float", "width": 140},
		{"fieldname": "payable_outstanding", "label": _("Buyer Payable"), "fieldtype": "Float", "width": 130},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 220},
	]
	companies = set(scope.companies(filters))
	to_date = filters.to_date or nowdate()
	data = []
	for seller, buyer in _pairs(companies):
		result = reconciliation(seller, buyer, to_date)
		data.append({"label": f"{seller} → {buyer}", "indent": 0, **{k: result[k] for k in ("invoiced_by_seller",
			"booked_by_buyer", "receivable_outstanding", "payable_outstanding")},
			"status": _("Balanced") if result["balanced"] else _("Differences")})
		for name in result["unmatched_sales_invoices"]:
			data.append({"label": name, "indent": 1, "status": _("Sales invoice not booked by buyer")})
		for name in result["unmatched_purchase_invoices"]:
			data.append({"label": name, "indent": 1, "status": _("Purchase invoice without seller invoice")})
		for sales, purchase, seller_amount, buyer_amount in result["amount_mismatches"]:
			data.append({"label": f"{sales} / {purchase}", "indent": 1, "invoiced_by_seller": seller_amount,
				"booked_by_buyer": buyer_amount, "status": _("Amounts differ")})
	return columns, data
