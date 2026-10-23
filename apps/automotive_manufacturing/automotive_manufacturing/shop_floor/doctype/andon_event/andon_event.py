# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_manufacturing.shop_floor import andon


class AndonEvent(Document):
	def validate(self):
		andon.validate(self)

	def after_insert(self):
		andon.after_insert(self)
