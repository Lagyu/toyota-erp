# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Where-used (ERP-02): every BOM, at any level, that consumes an item — the impact
list an engineering change or a supplier problem starts from."""

import frappe
from frappe import _

from automotive_manufacturing.product_engineering.change_control import where_used


def execute(filters=None):
	filters = frappe._dict(filters or {})
	columns = [
		{"fieldname": "level", "label": _("Level"), "fieldtype": "Int", "width": 70},
		{"fieldname": "bom", "label": _("BOM"), "fieldtype": "Link", "options": "BOM", "width": 200},
		{"fieldname": "parent_item", "label": _("Used In"), "fieldtype": "Link", "options": "Item", "width": 180},
		{"fieldname": "qty", "label": _("Qty per Parent"), "fieldtype": "Float", "width": 110},
		{"fieldname": "revision", "label": _("Revision"), "fieldtype": "Data", "width": 90},
		{"fieldname": "effective_from", "label": _("Effective From"), "fieldtype": "Date", "width": 110},
		{"fieldname": "effective_to", "label": _("Effective To"), "fieldtype": "Date", "width": 110},
		{"fieldname": "is_default", "label": _("Default"), "fieldtype": "Check", "width": 70},
		{"fieldname": "path", "label": _("Path"), "fieldtype": "Data", "width": 360},
	]
	if not filters.item_code:
		return columns, []
	frappe.has_permission("BOM", "read", throw=True)
	rows = where_used(filters.item_code, released_only=not filters.get("include_drafts"))
	meta = {b.name: b for b in frappe.get_all("BOM", filters={"name": ("in", [r["bom"] for r in rows] or [""])},
		fields=["name", "is_default", "automotive_revision", "automotive_effective_from", "automotive_effective_to"])}
	for row in rows:
		bom = meta.get(row["bom"]) or frappe._dict()
		row.update({"indent": row["level"] - 1, "revision": bom.automotive_revision,
			"effective_from": bom.automotive_effective_from, "effective_to": bom.automotive_effective_to,
			"is_default": bom.is_default})
	return columns, rows
