# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_manufacturing.product_engineering import change_control


class EngineeringChangeOrder(Document):
	def validate(self):
		change_control.validate_eco(self)

	def on_submit(self):
		change_control.implement(self)
		if self.engineering_change_request:
			import frappe

			frappe.db.set_value(
				"Engineering Change Request", self.engineering_change_request, "status", "Converted"
			)

	def on_cancel(self):
		change_control.revert(self)
