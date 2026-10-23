# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_core.governance import delegation


class ApprovalDelegation(Document):
	def validate(self):
		delegation.validate(self)

	def on_submit(self):
		delegation.on_submit(self)

	def on_cancel(self):
		delegation.on_cancel(self)
