# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Purchase price variance (ERP-09): invoiced price against the item's standard cost and
against the ordered price, per submitted supplier invoice line, in company currency."""

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate

from automotive_core.analytics import scope


def execute(filters=None):
	filters = frappe._dict(filters or {})
	to_date = getdate(filters.to_date or nowdate())
	from_date = getdate(filters.from_date or add_days(to_date, -29))
	columns = [
		{
			"fieldname": "purchase_invoice",
			"label": _("Invoice"),
			"fieldtype": "Link",
			"options": "Purchase Invoice",
			"width": 160,
		},
		{"fieldname": "posting_date", "label": _("Date"), "fieldtype": "Date", "width": 95},
		{
			"fieldname": "supplier",
			"label": _("Supplier"),
			"fieldtype": "Link",
			"options": "Supplier",
			"width": 170,
		},
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 150},
		{"fieldname": "stock_qty", "label": _("Qty"), "fieldtype": "Float", "width": 80},
		{"fieldname": "invoiced_rate", "label": _("Invoiced Rate"), "fieldtype": "Currency", "width": 115},
		{"fieldname": "ordered_rate", "label": _("Ordered Rate"), "fieldtype": "Currency", "width": 115},
		{"fieldname": "standard_cost", "label": _("Standard Cost"), "fieldtype": "Currency", "width": 115},
		{
			"fieldname": "ppv_vs_standard",
			"label": _("PPV vs Standard"),
			"fieldtype": "Currency",
			"width": 130,
		},
		{"fieldname": "ppv_vs_order", "label": _("Variance vs Order"), "fieldtype": "Currency", "width": 130},
		{
			"fieldname": "company",
			"label": _("Company"),
			"fieldtype": "Link",
			"options": "Company",
			"width": 180,
		},
	]
	companies = scope.companies(filters)
	values = {"companies": scope.sql_in(companies), "from": from_date, "to": to_date}
	extra = ""
	if filters.supplier:
		extra += " and pi.supplier = %(supplier)s"
		values["supplier"] = filters.supplier
	if filters.item_code:
		extra += " and pii.item_code = %(item)s"
		values["item"] = filters.item_code
	rows = frappe.db.sql(
		f"""select pi.name purchase_invoice, pi.posting_date, pi.supplier, pi.company, pii.item_code,
			pii.stock_qty, pii.base_net_rate / ifnull(nullif(pii.conversion_factor, 0), 1) invoiced_rate,
			poi.base_net_rate / ifnull(nullif(poi.conversion_factor, 0), 1) ordered_rate,
			i.automotive_standard_cost standard_cost
		from `tabPurchase Invoice Item` pii join `tabPurchase Invoice` pi on pi.name = pii.parent
		join `tabItem` i on i.name = pii.item_code
		left join `tabPurchase Order Item` poi on poi.name = pii.po_detail
		where pi.docstatus = 1 and pi.is_return = 0 and pi.company in %(companies)s
		and pi.posting_date between %(from)s and %(to)s {extra}
		order by pi.posting_date, pi.name, pii.idx""",  # noqa: S608 (constant SQL fragments; values are bound)
		values,
		as_dict=True,
	)
	for row in rows:
		qty = flt(row.stock_qty)
		row["ppv_vs_standard"] = (
			(flt(row.invoiced_rate) - flt(row.standard_cost)) * qty if row.standard_cost else None
		)
		row["ppv_vs_order"] = (
			(flt(row.invoiced_rate) - flt(row.ordered_rate)) * qty if row.ordered_rate else None
		)
	return columns, rows
