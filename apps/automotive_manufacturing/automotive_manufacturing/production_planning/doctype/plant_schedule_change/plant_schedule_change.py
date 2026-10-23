# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PlantScheduleChange(Document):
	def validate(self):
		if not self.is_new():
			raise frappe.PermissionError(_("Schedule change records are append-only."))

	def on_trash(self):
		raise frappe.PermissionError(_("Schedule change records cannot be deleted."))
