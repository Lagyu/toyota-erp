# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_supply_chain.supplier_collaboration import schedules


class SupplierDeliverySchedule(Document):
	def before_insert(self):
		schedules.before_insert_schedule(self)

	def validate(self):
		if self.docstatus == 0:
			self.status = "Draft"

	def on_submit(self):
		schedules.on_schedule_submit(self)
