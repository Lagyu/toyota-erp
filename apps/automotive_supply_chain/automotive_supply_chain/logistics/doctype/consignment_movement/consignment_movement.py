# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_supply_chain.logistics import consignment


class ConsignmentMovement(Document):
	def validate(self):
		consignment.validate_movement(self)

	def on_submit(self):
		consignment.on_movement_submit(self)

	def on_cancel(self):
		consignment.on_movement_cancel(self)
