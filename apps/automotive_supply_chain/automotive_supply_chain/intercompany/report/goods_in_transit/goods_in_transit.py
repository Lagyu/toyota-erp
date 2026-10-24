# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Goods in transit between group companies (ENT-5, ERP-06.2): intercompany deliveries the
seller has shipped and the buyer has not received by the given date, valued in the buyer's
currency at the transfer price. Goods the buyer has already been invoiced for are what it
carries in Stock Received But Not Billed until the receipt."""

import frappe
from frappe import _
from frappe.utils import date_diff, getdate, nowdate

from automotive_core.analytics import scope


def in_transit(as_on, companies: set[str] | None = None) -> list[dict]:
	rows = frappe.db.sql("""select dn.name delivery_note, dn.company seller, c.represents_company buyer,
			dn.posting_date shipped_on, dn.total_qty qty, dn.net_total value, dn.currency,
			(select pi.name from `tabSales Invoice Item` sii
				join `tabPurchase Invoice` pi on pi.inter_company_invoice_reference = sii.parent and pi.docstatus = 1
					and pi.posting_date <= %(as_on)s
				where sii.delivery_note = dn.name and sii.docstatus = 1 limit 1) buyer_invoice
		from `tabDelivery Note` dn join `tabCustomer` c on c.name = dn.customer
		where dn.docstatus = 1 and dn.is_return = 0 and c.is_internal_customer = 1 and dn.posting_date <= %(as_on)s
			and not exists (select 1 from `tabPurchase Receipt` pr where pr.inter_company_reference = dn.name
				and pr.docstatus = 1 and pr.posting_date <= %(as_on)s)
		order by dn.posting_date, dn.name""", {"as_on": as_on}, as_dict=True)
	return [r for r in rows if companies is None or r.seller in companies or r.buyer in companies]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	as_on = getdate(filters.as_on_date or nowdate())
	columns = [
		{"fieldname": "delivery_note", "label": _("Delivery Note"), "fieldtype": "Link", "options": "Delivery Note",
			"width": 170},
		{"fieldname": "seller", "label": _("Seller"), "fieldtype": "Link", "options": "Company", "width": 230},
		{"fieldname": "buyer", "label": _("Buyer"), "fieldtype": "Link", "options": "Company", "width": 230},
		{"fieldname": "shipped_on", "label": _("Shipped On"), "fieldtype": "Date", "width": 110},
		{"fieldname": "days", "label": _("Days In Transit"), "fieldtype": "Int", "width": 120},
		{"fieldname": "qty", "label": _("Qty"), "fieldtype": "Float", "width": 80},
		{"fieldname": "value", "label": _("Value (Buyer Currency)"), "fieldtype": "Currency", "options": "currency",
			"width": 170},
		{"fieldname": "currency", "label": _("Currency"), "fieldtype": "Link", "options": "Currency", "hidden": 1},
		{"fieldname": "buyer_invoice", "label": _("Buyer Invoice"), "fieldtype": "Link", "options": "Purchase Invoice",
			"width": 170},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 190},
	]
	data = []
	for row in in_transit(as_on, set(scope.companies(filters))):
		row.days = date_diff(as_on, row.shipped_on)
		row.status = _("Invoiced, not received") if row.buyer_invoice else _("Shipped, not invoiced")
		data.append(row)
	return columns, data
