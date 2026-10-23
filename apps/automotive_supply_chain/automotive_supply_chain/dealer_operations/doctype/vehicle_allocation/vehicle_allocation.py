# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class VehicleAllocation(Document):
	def validate(self):
		if not self.is_new() and not self.flags.get("ignore_permissions"):
			raise frappe.PermissionError(_("Allocations change through allocate / release."))
