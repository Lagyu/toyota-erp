# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class VehicleOptionRule(Document):
	def validate(self):
		attributes = set(frappe.get_all("Vehicle Model Option Group", filters={"parent": self.vehicle_model},
			pluck="attribute"))
		for field in ("if_attribute", "then_attribute"):
			value = self.get(field)
			if value and value not in attributes:
				frappe.throw(_("{0} is not an option group of {1}.").format(value, self.vehicle_model))
		if self.rule_type != "Allowed Values" and not self.if_attribute:
			frappe.throw(_("{0} rules need a condition.").format(self.rule_type))
		for field, attribute in (("if_values", self.if_attribute), ("then_values", self.then_attribute)):
			if not attribute:
				continue
			known = set(frappe.get_all("Item Attribute Value", filters={"parent": attribute}, pluck="attribute_value"))
			for value in filter(None, (v.strip() for v in (self.get(field) or "").splitlines())):
				if value not in known:
					frappe.throw(_("{0} is not a value of {1}.").format(value, attribute))
