# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_supply_chain.dealer_operations import orders


class DealerRegistration(Document):
	def validate(self):
		orders.validate_registration(self)

	def on_submit(self):
		orders.on_registration_submit(self)

	def on_cancel(self):
		self.db_set("status", "Rejected")
