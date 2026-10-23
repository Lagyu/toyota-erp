# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_supply_chain.intercompany import transfer_pricing


class TransferPriceAgreement(Document):
	def validate(self):
		transfer_pricing.validate_agreement(self)
