# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_supply_chain.dealer_operations import orders


class DealerOrderChange(Document):
	def validate(self):
		orders.validate_change(self)

	def on_submit(self):
		effect = orders.apply_change(self)
		self.db_set({"status": "Applied", "effect": effect})
