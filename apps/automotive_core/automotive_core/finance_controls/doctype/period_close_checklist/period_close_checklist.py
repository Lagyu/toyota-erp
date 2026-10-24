# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_core.finance_controls import period_close


class PeriodCloseChecklist(Document):
	def validate(self):
		period_close.validate(self)

	def before_submit(self):
		period_close.before_submit(self)

	def on_submit(self):
		period_close.on_submit(self)

	def on_cancel(self):
		period_close.on_cancel(self)
