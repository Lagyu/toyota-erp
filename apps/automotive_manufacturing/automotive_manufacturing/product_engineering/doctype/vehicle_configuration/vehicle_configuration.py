# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_manufacturing.product_engineering.configuration import apply_to_document


class VehicleConfiguration(Document):
	def validate(self):
		apply_to_document(self)
