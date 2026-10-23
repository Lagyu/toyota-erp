# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class ApprovalDecision(Document):
	def validate(self):
		if not self.is_new():
			raise frappe.PermissionError(_("Approval Decisions are immutable."))
		if not self.flags.get("automotive_approval_service"):
			raise frappe.PermissionError(_("Approval Decisions are recorded only by the approval service."))

	def on_trash(self):
		raise frappe.PermissionError(_("Approval Decisions cannot be deleted."))
