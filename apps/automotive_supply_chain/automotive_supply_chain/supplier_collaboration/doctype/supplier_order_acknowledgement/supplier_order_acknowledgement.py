# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_supply_chain.partners import assert_party_access
from automotive_supply_chain.supplier_collaboration import schedules


class SupplierOrderAcknowledgement(Document):
	def validate(self):
		schedules.validate_acknowledgement(self)
		assert_party_access("Supplier", self.supplier)

	def on_submit(self):
		schedules.on_acknowledgement_submit(self)
