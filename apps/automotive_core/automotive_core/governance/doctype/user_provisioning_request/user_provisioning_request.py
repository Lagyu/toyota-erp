# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_core.governance import provisioning


class UserProvisioningRequest(Document):
	def validate(self):
		provisioning.validate(self)

	def on_submit(self):
		provisioning.on_submit(self)

	def on_cancel(self):
		self.db_set("status", "Cancelled")
