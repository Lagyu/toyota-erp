# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class InvoiceMatchException(Document):
	def on_trash(self):
		raise frappe.PermissionError(_("Match exceptions are kept as audit evidence."))
