# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class SegregationofDutiesRule(Document):
	def validate(self):
		if self.role_a == self.role_b:
			frappe.throw(_("A rule needs two different roles."))
