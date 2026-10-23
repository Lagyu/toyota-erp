# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_supply_chain.supplier_collaboration import sourcing


class SupplierOnboarding(Document):
	def validate(self):
		sourcing.validate_onboarding(self)

	def on_submit(self):
		sourcing.on_onboarding_submit(self)

	def on_cancel(self):
		self.db_set("status", "Rejected")
