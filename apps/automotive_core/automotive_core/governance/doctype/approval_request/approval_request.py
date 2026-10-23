# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ApprovalRequest(Document):
	def validate(self):
		if not self.flags.get("automotive_approval_service"):
			raise frappe.PermissionError(_("Approval Requests are changed only by the approval service."))

	def on_trash(self):
		if not frappe.flags.in_test:
			raise frappe.PermissionError(_("Approval history cannot be deleted."))
