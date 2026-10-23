# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe.model.document import Document

from automotive_manufacturing.product_engineering.change_control import assess


class EngineeringChangeRequest(Document):
	def validate(self):
		if self.is_new():
			self.requested_by = frappe.session.user
		if self.docstatus == 0:
			self.status = "Draft"

	def before_submit(self):
		self.update(assess(self))

	def on_submit(self):
		self.db_set("status", "Under Review")

	def on_cancel(self):
		self.db_set("status", "Rejected")
