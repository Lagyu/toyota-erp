# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PlanningRun(Document):
	def validate(self):
		if (
			self.parameter_set
			and frappe.db.get_value("Planning Parameter Set", self.parameter_set, "docstatus") != 1
		):
			frappe.throw(
				_("Parameter set {0} is not an approved (submitted) version.").format(self.parameter_set)
			)
		if not self.is_new() and self.status in ("Released",) and not self.flags.get("ignore_permissions"):
			before = self.get_doc_before_save()
			if before and before.status == "Released":
				frappe.throw(_("A released run is final; start a new run."))
