# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Affected vehicles (ERP-07, J-05 recall scope): every finished unit that contains the
given component — optionally only specific lots/serials or one supplier's parts — with
where each vehicle is now (in stock, in transit, at which dealer)."""

import frappe
from frappe import _

from automotive_core.analytics import scope
from automotive_manufacturing.traceability.genealogy import units_containing


def _split(text: str | None) -> list[str]:
	return [v.strip() for v in (text or "").replace(",", "\n").splitlines() if v.strip()]


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = [
		{
			"fieldname": "serial_no",
			"label": _("VIN / Serial No"),
			"fieldtype": "Link",
			"options": "Serial No",
			"width": 170,
		},
		{"fieldname": "item_code", "label": _("Item"), "fieldtype": "Link", "options": "Item", "width": 160},
		{
			"fieldname": "company",
			"label": _("Company"),
			"fieldtype": "Link",
			"options": "Company",
			"width": 200,
		},
		{"fieldname": "inventory_status", "label": _("Inventory"), "fieldtype": "Data", "width": 100},
		{"fieldname": "shipment_status", "label": _("Shipment"), "fieldtype": "Data", "width": 100},
		{
			"fieldname": "dealer",
			"label": _("Dealer"),
			"fieldtype": "Link",
			"options": "Customer",
			"width": 170,
		},
		{
			"fieldname": "current_warehouse",
			"label": _("Warehouse"),
			"fieldtype": "Link",
			"options": "Warehouse",
			"width": 200,
		},
		{"fieldname": "path", "label": _("Trace"), "fieldtype": "Data", "width": 420},
	]
	if not (filters.item_code or filters.batch_nos or filters.serial_nos or filters.supplier):
		return columns, []
	frappe.has_permission("Genealogy Link", "read", throw=True)
	allowed = set(scope.companies(filters))
	units = units_containing(
		filters.item_code, _split(filters.batch_nos), _split(filters.serial_nos), filters.supplier
	)
	rows = []
	for unit in units:
		vehicle = (
			frappe.db.get_value(
				"Vehicle Unit",
				unit["serial_no"],
				["company", "inventory_status", "shipment_status", "dealer", "current_warehouse"],
				as_dict=True,
			)
			or frappe._dict()
		)
		company = vehicle.company or frappe.db.get_value("Serial No", unit["serial_no"], "company")
		if company and company not in allowed:
			continue
		rows.append({**unit, **vehicle, "company": company})
	return columns, rows
