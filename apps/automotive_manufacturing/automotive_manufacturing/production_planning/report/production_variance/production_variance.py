# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Production variance (ERP-04, ERP-09): per work order, quantity built against plan and
the material actually consumed (valuation of the Manufacture entries' raw-material rows)
against the standard (BOM cost of the units built)."""

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate

from automotive_core.analytics import scope


def execute(filters=None):
	filters = frappe._dict(filters or {})
	to_date = getdate(filters.to_date or nowdate())
	from_date = getdate(filters.from_date or add_days(to_date, -29))
	columns = [
		{"fieldname": "work_order", "label": _("Work Order"), "fieldtype": "Link", "options": "Work Order", "width": 170},
		{"fieldname": "production_item", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 170},
		{"fieldname": "production_line", "label": _("Line"), "fieldtype": "Link", "options": "Production Line",
			"width": 110},
		{"fieldname": "planned_qty", "label": _("Planned"), "fieldtype": "Float", "width": 80},
		{"fieldname": "produced_qty", "label": _("Built"), "fieldtype": "Float", "width": 80},
		{"fieldname": "qty_variance", "label": _("Qty Variance"), "fieldtype": "Float", "width": 100},
		{"fieldname": "standard_cost", "label": _("Standard Material"), "fieldtype": "Currency", "width": 140},
		{"fieldname": "actual_cost", "label": _("Actual Material"), "fieldtype": "Currency", "width": 140},
		{"fieldname": "cost_variance", "label": _("Material Variance"), "fieldtype": "Currency", "width": 140},
		{"fieldname": "variance_percent", "label": _("Variance %"), "fieldtype": "Percent", "width": 100},
	]
	companies = scope.companies(filters)
	plants = scope.plants(filters, company_list=companies)
	orders = frappe.db.sql("""select wo.name, wo.production_item, wo.automotive_production_line production_line,
			wo.qty, wo.produced_qty, wo.bom_no, b.total_cost / b.quantity unit_cost
		from `tabWork Order` wo join `tabBOM` b on b.name = wo.bom_no
		where wo.docstatus = 1 and wo.company in %(companies)s and wo.automotive_plant in %(plants)s
		and exists (select 1 from `tabStock Entry` se where se.work_order = wo.name and se.docstatus = 1
			and se.purpose = 'Manufacture' and se.posting_date between %(from)s and %(to)s)
		order by wo.name""", {"companies": scope.sql_in(companies), "plants": scope.sql_in(plants),
		"from": from_date, "to": to_date}, as_dict=True)
	actual = {}
	if orders:
		for row in frappe.db.sql("""select se.work_order, sum(sed.basic_amount) consumed
				from `tabStock Entry` se join `tabStock Entry Detail` sed on sed.parent = se.name
				where se.docstatus = 1 and se.purpose = 'Manufacture' and se.work_order in %s
				and sed.is_finished_item = 0 and ifnull(sed.s_warehouse, '') != ''
				group by se.work_order""", [tuple(o.name for o in orders)], as_dict=True):
			actual[row.work_order] = flt(row.consumed)
	data = []
	for o in orders:
		standard = flt(o.unit_cost) * flt(o.produced_qty)
		consumed = actual.get(o.name, 0.0)
		data.append({"work_order": o.name, "production_item": o.production_item, "production_line": o.production_line,
			"planned_qty": o.qty, "produced_qty": o.produced_qty, "qty_variance": flt(o.produced_qty) - flt(o.qty),
			"standard_cost": standard, "actual_cost": consumed, "cost_variance": consumed - standard,
			"variance_percent": round((consumed - standard) / standard * 100, 2) if standard else None})
	return columns, data
