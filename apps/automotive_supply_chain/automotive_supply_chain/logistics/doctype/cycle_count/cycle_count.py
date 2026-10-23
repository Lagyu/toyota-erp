# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_supply_chain.logistics import operations


class CycleCount(Document):
	def validate(self):
		operations.validate_count(self)

	def on_submit(self):
		operations.on_count_submit(self)
