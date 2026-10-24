# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Inventory position (ERP-06): physical, reserved, projected and valuation views of the
same stock, side by side, from ERPNext Bins (which the stock ledger maintains)."""

import frappe
from frappe import _

from automotive_core.analytics import scope

LIMIT = 5000


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = [
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 160},
		{"fieldname": "item_name", "label": _("Item Name"), "fieldtype": "Data", "width": 180},
		{
			"fieldname": "warehouse",
			"label": _("Warehouse"),
			"fieldtype": "Link",
			"options": "Warehouse",
			"width": 220,
		},
		{"fieldname": "role", "label": _("Role"), "fieldtype": "Data", "width": 100},
		{"fieldname": "actual_qty", "label": _("Physical"), "fieldtype": "Float", "width": 95},
		{"fieldname": "reserved_stock", "label": _("Reserved (Stock)"), "fieldtype": "Float", "width": 120},
		{"fieldname": "reserved_qty", "label": _("Reserved (Sales)"), "fieldtype": "Float", "width": 120},
		{
			"fieldname": "reserved_qty_for_production",
			"label": _("Reserved (Production)"),
			"fieldtype": "Float",
			"width": 150,
		},
		{"fieldname": "available_qty", "label": _("Available"), "fieldtype": "Float", "width": 95},
		{"fieldname": "ordered_qty", "label": _("On Order"), "fieldtype": "Float", "width": 90},
		{"fieldname": "projected_qty", "label": _("Projected"), "fieldtype": "Float", "width": 95},
		{"fieldname": "valuation_rate", "label": _("Valuation Rate"), "fieldtype": "Currency", "width": 120},
		{"fieldname": "stock_value", "label": _("Stock Value"), "fieldtype": "Currency", "width": 130},
	]
	companies = scope.companies(filters)
	plants = scope.plants(filters, company_list=companies)
	values = {"companies": scope.sql_in(companies), "plants": scope.sql_in(plants), "limit": LIMIT}
	clauses = [
		"w.company in %(companies)s",
		"(ifnull(w.automotive_plant, '') = '' or w.automotive_plant in %(plants)s)",
	]
	if filters.get("plant"):
		clauses.append("w.automotive_plant = %(plant)s")
		values["plant"] = filters.plant
	for key, column in (
		("warehouse", "b.warehouse"),
		("item_code", "b.item_code"),
		("item_group", "i.item_group"),
	):
		if filters.get(key):
			clauses.append(f"{column} = %({key})s")
			values[key] = filters.get(key)
	if not filters.get("include_zero"):
		clauses.append("(b.actual_qty != 0 or b.ordered_qty != 0 or b.reserved_qty != 0)")
	where = " and ".join(clauses)
	rows = frappe.db.sql(
		f"""select b.item_code, i.item_name, b.warehouse, w.automotive_warehouse_role role,
			b.actual_qty, b.reserved_stock, b.reserved_qty, b.reserved_qty_for_production, b.ordered_qty,
			b.projected_qty, b.valuation_rate, b.stock_value
		from `tabBin` b join `tabWarehouse` w on w.name = b.warehouse join `tabItem` i on i.name = b.item_code
		where {where} order by b.item_code, b.warehouse limit %(limit)s""",  # noqa: S608 (constant SQL fragments; values are bound)
		values,
		as_dict=True,
	)
	for row in rows:
		row["available_qty"] = row.actual_qty - row.reserved_stock
	return columns, rows
