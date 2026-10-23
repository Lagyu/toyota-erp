# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from automotive_core.governance.approvals import clear_policy_cache


class ApprovalPolicy(Document):
	def validate(self):
		meta = frappe.get_meta(self.reference_doctype)
		if not meta.is_submittable:
			frappe.throw(_("{0} is not submittable; approval policies govern submission.").format(self.reference_doctype))
		if self.amount_field and not meta.has_field(self.amount_field):
			frappe.throw(_("{0} has no field {1}.").format(self.reference_doctype, self.amount_field))
		if self.category_field:
			table, _sep, column = self.category_field.partition(".")
			if column:
				child = meta.get_field(table)
				if not child or child.fieldtype != "Table" or not frappe.get_meta(child.options).has_field(column):
					frappe.throw(_("{0} is not a valid child-table field path.").format(self.category_field))
			elif not meta.has_field(table):
				frappe.throw(_("{0} has no field {1}.").format(self.reference_doctype, table))
		if self.max_amount and self.min_amount and self.max_amount < self.min_amount:
			frappe.throw(_("Applies Up To Amount must be at least Applies From Amount."))
		numbers = [s.step_no for s in self.steps]
		if len(numbers) != len(set(numbers)):
			frappe.throw(_("Step numbers must be unique."))
		self.steps.sort(key=lambda s: s.step_no)
		for idx, step in enumerate(self.steps, start=1):
			step.idx = idx

	def on_update(self):
		clear_policy_cache()

	def on_trash(self):
		if frappe.db.exists("Approval Request", {"policy": self.name}):
			frappe.throw(_("Policy {0} has approval history; deactivate it instead.").format(self.name))
		clear_policy_cache()
