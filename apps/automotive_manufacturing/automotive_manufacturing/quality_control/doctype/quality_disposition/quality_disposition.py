# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_manufacturing.quality_control import dispositions


class QualityDisposition(Document):
	def validate(self):
		dispositions.validate(self)

	def on_submit(self):
		dispositions.execute(self)

	def on_cancel(self):
		import frappe
		from frappe import _

		frappe.throw(_("An executed disposition is part of the stock history; record a new disposition instead."))
