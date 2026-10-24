# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class VehicleModel(Document):
	def validate(self):
		self.model_code = (self.model_code or "").strip().upper()
		if not frappe.db.get_value("Item", self.template_item, "has_variants"):
			frappe.throw(_("Template item {0} must have variants enabled.").format(self.template_item))
		template_attributes = set(
			frappe.get_all(
				"Item Variant Attribute", filters={"parent": self.template_item}, pluck="attribute"
			)
		)
		groups = {g.attribute for g in self.option_groups}
		if groups and groups != template_attributes:
			frappe.throw(
				_("Option groups {0} must match the template's variant attributes {1}.").format(
					sorted(groups), sorted(template_attributes)
				)
			)
		if frappe.db.get_value("Production Line", self.production_line, "plant") != self.default_plant:
			frappe.throw(_("Line {0} is not in plant {1}.").format(self.production_line, self.default_plant))
		if self.vin_prefix:
			from automotive_manufacturing.engine.vin import TRANSLITERATION

			self.vin_prefix = self.vin_prefix.upper()
			if len(self.vin_prefix) != 8 or any(c not in TRANSLITERATION for c in self.vin_prefix):
				frappe.throw(_("VIN prefix must be 8 VIN characters (no I, O or Q)."))
