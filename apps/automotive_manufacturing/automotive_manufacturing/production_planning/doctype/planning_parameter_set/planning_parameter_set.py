# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class PlanningParameterSet(Document):
	def before_insert(self):
		# Versions are new documents (format autoname), not Frappe "amended" copies.
		if self.amended_from and not self.previous_version:
			self.previous_version = self.amended_from
		self.amended_from = None
		latest = frappe.db.sql(
			"select max(version) from `tabPlanning Parameter Set` where parameter_set=%s", self.parameter_set
		)[0][0]
		self.version = int(latest or 0) + 1

	def validate(self):
		if int(self.horizon_days or 0) <= 0 or int(self.horizon_days) > 400:
			frappe.throw(_("Horizon must be between 1 and 400 days."))
		if frappe.db.get_value("Plant", self.plant, "company") != self.company:
			frappe.throw(_("Plant {0} is not in {1}.").format(self.plant, self.company))


@frappe.whitelist(methods=["POST"])
def new_version(name: str) -> str:
	source = frappe.get_doc("Planning Parameter Set", name)
	source.check_permission("read")
	copy = frappe.copy_doc(source)
	copy.previous_version = source.name
	copy.change_note = None
	copy.insert()
	return copy.name
