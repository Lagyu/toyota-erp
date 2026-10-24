# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Work in progress by production line (ERP-04): open work orders and the value of
material staged at line side / in WIP, straight from Work Orders and Bins."""

import frappe
from frappe import _
from frappe.utils import date_diff, flt, nowdate

from automotive_core.analytics import scope

OPEN = ("Not Started", "In Process")


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = [
		{"fieldname": "production_line", "label": _("Line / Work Order"), "fieldtype": "Data", "width": 220},
		{"fieldname": "plant", "label": _("Plant"), "fieldtype": "Link", "options": "Plant", "width": 110},
		{"fieldname": "item", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 160},
		{"fieldname": "open_orders", "label": _("Open Orders"), "fieldtype": "Int", "width": 100},
		{"fieldname": "planned_qty", "label": _("Planned"), "fieldtype": "Float", "width": 90},
		{"fieldname": "built_qty", "label": _("Built"), "fieldtype": "Float", "width": 90},
		{"fieldname": "remaining_qty", "label": _("Remaining"), "fieldtype": "Float", "width": 100},
		{"fieldname": "oldest_days", "label": _("Oldest (Days)"), "fieldtype": "Int", "width": 100},
		{
			"fieldname": "line_side_value",
			"label": _("Line-Side Stock Value"),
			"fieldtype": "Currency",
			"width": 160,
		},
		{"fieldname": "wip_value", "label": _("WIP Warehouse Value"), "fieldtype": "Currency", "width": 150},
	]
	companies = scope.companies(filters)
	plants = scope.plants(filters, company_list=companies)
	line_filters = {"plant": ("in", plants or [""])}
	if filters.production_line:
		line_filters["name"] = filters.production_line
	today = nowdate()
	data = []
	for line in frappe.get_all(
		"Production Line",
		filters=line_filters,
		fields=["name", "plant", "line_side_warehouse", "wip_warehouse"],
		order_by="name",
	):
		orders = frappe.get_all(
			"Work Order",
			filters={"automotive_production_line": line.name, "docstatus": 1, "status": ("in", OPEN)},
			fields=["name", "production_item", "qty", "produced_qty", "planned_start_date"],
			order_by="planned_start_date asc",
		)
		values = {}
		for key in ("line_side_warehouse", "wip_warehouse"):
			values[key] = (
				flt(
					frappe.db.sql(
						"select sum(stock_value) from `tabBin` where warehouse = %s", line.get(key)
					)[0][0]
				)
				if line.get(key)
				else 0
			)
		data.append(
			{
				"production_line": line.name,
				"plant": line.plant,
				"indent": 0,
				"open_orders": len(orders),
				"planned_qty": sum(flt(o.qty) for o in orders),
				"built_qty": sum(flt(o.produced_qty) for o in orders),
				"remaining_qty": sum(flt(o.qty) - flt(o.produced_qty) for o in orders),
				"oldest_days": max(
					[date_diff(today, o.planned_start_date) for o in orders if o.planned_start_date] or [0]
				),
				"line_side_value": values["line_side_warehouse"],
				"wip_value": values["wip_warehouse"],
			}
		)
		if filters.get("show_orders"):
			for o in orders:
				data.append(
					{
						"production_line": o.name,
						"plant": line.plant,
						"item": o.production_item,
						"indent": 1,
						"planned_qty": o.qty,
						"built_qty": o.produced_qty,
						"remaining_qty": flt(o.qty) - flt(o.produced_qty),
						"oldest_days": date_diff(today, o.planned_start_date)
						if o.planned_start_date
						else None,
					}
				)
	return columns, data
