# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Segregation-of-duties review (ERP-10): every enabled user whose roles currently break
an active SoD rule, with the rule's enforcement level, for periodic access reviews."""

import frappe
from frappe import _

from automotive_core.governance.sod import report


def execute(filters=None):
	columns = [
		{"fieldname": "user", "label": _("User"), "fieldtype": "Link", "options": "User", "width": 260},
		{"fieldname": "full_name", "label": _("Name"), "fieldtype": "Data", "width": 180},
		{"fieldname": "rule", "label": _("Rule"), "fieldtype": "Link", "options": "Segregation of Duties Rule",
			"width": 220},
		{"fieldname": "role_a", "label": _("Role"), "fieldtype": "Link", "options": "Role", "width": 170},
		{"fieldname": "role_b", "label": _("Conflicting Role"), "fieldtype": "Link", "options": "Role", "width": 170},
		{"fieldname": "enforcement", "label": _("Enforcement"), "fieldtype": "Data", "width": 110},
	]
	rows = report()
	names = dict(frappe.get_all("User", filters={"name": ("in", [r["user"] for r in rows] or [""])},
		fields=["name", "full_name"], as_list=True))
	for row in rows:
		row["full_name"] = names.get(row["user"])
	return columns, rows
