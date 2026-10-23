# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_supply_chain.supplier_collaboration import sourcing


class SourcingDecision(Document):
	def validate(self):
		sourcing.score(self)

	def on_submit(self):
		sourcing.on_sourcing_submit(self)
