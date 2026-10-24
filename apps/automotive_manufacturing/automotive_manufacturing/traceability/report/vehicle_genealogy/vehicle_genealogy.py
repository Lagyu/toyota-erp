# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Vehicle genealogy (ERP-07): the components (serials and lots, with their suppliers)
a vehicle or other serialized unit was built from, level by level."""

import frappe
from frappe import _

from automotive_core.analytics import scope
from automotive_manufacturing.traceability.genealogy import components_of


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = [
		{"fieldname": "level", "label": _("Level"), "fieldtype": "Int", "width": 70},
		{"fieldname": "parent", "label": _("Built Into"), "fieldtype": "Data", "width": 170},
		{"fieldname": "component_item", "label": _("Component"), "fieldtype": "Link", "options": "Item", "width": 170},
		{"fieldname": "component_serial_no", "label": _("Serial No"), "fieldtype": "Link", "options": "Serial No",
			"width": 170},
		{"fieldname": "component_batch_no", "label": _("Batch / Lot"), "fieldtype": "Link", "options": "Batch",
			"width": 160},
		{"fieldname": "qty", "label": _("Qty"), "fieldtype": "Float", "width": 70},
		{"fieldname": "supplier", "label": _("Supplier"), "fieldtype": "Link", "options": "Supplier", "width": 170},
		{"fieldname": "supplier_lot", "label": _("Supplier Lot"), "fieldtype": "Data", "width": 120},
		{"fieldname": "stock_entry", "label": _("Build Entry"), "fieldtype": "Link", "options": "Stock Entry",
			"width": 150},
	]
	if not filters.serial_no:
		return columns, []
	frappe.has_permission("Genealogy Link", "read", throw=True)
	unit = frappe.db.get_value("Vehicle Unit", filters.serial_no, ["company", "plant"], as_dict=True)
	if unit:
		scope.companies({"company": unit.company})  # refuses vehicles of companies outside the viewer's scope
	rows = components_of(filters.serial_no)
	for row in rows:
		row["indent"] = row["level"] - 1
	return columns, rows
