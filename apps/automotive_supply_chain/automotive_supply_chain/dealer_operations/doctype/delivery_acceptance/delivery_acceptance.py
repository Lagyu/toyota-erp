# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_supply_chain.dealer_operations import fulfilment


class DeliveryAcceptance(Document):
	def validate(self):
		fulfilment.validate_acceptance(self)

	def on_submit(self):
		fulfilment.on_acceptance_submit(self)
