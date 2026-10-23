# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class AuditEvent(Document):
	def validate(self):
		if not self.is_new():
			raise frappe.PermissionError(_("Audit Events are append-only."))
		if not self.flags.get("automotive_audit_insert"):
			raise frappe.PermissionError(_("Audit Events are written only by the audit service."))

	def on_trash(self):
		raise frappe.PermissionError(_("Audit Events cannot be deleted."))
