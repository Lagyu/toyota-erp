# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class GenealogyLink(Document):
	"""Written only by stock entry hooks (db_insert); never edited by users."""

	def validate(self):
		raise frappe.PermissionError(_("Genealogy links are recorded automatically from stock entries."))
