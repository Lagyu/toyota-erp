# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_supply_chain.supplier_collaboration import sourcing


class ApprovedSupplierList(Document):
	def validate(self):
		sourcing.validate_asl(self)
