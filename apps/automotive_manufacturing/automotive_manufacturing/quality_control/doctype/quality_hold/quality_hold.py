# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_manufacturing.quality_control import holds


class QualityHold(Document):
	def validate(self):
		holds.validate_hold(self)

	def after_insert(self):
		holds.on_hold_insert(self)
