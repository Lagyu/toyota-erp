# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from automotive_manufacturing.shop_floor.kanban import ensure_cards, recommended_cards


class KanbanLoop(Document):
	def validate(self):
		self.loop_code = (self.loop_code or "").strip().upper()
		if frappe.get_cached_value("Warehouse", self.line_side_warehouse, "automotive_warehouse_role") != "Line Side":
			frappe.throw(_("{0} is not a line-side warehouse.").format(self.line_side_warehouse))
		plant = frappe.get_cached_value("Warehouse", self.line_side_warehouse, "automotive_plant")
		if plant != self.plant:
			frappe.throw(_("{0} belongs to plant {1}.").format(self.line_side_warehouse, plant))
		if self.supply_type == "Internal Transfer" and self.source_warehouse == self.line_side_warehouse:
			frappe.throw(_("Source and line-side warehouse must differ."))
		if self.flags.get("ignore_card_reduction") is None and not self.is_new():
			before = self.get_doc_before_save()
			if before and self.number_of_cards < before.number_of_cards:
				in_flight = frappe.db.count("Kanban Card", {"kanban_loop": self.name, "status": ("!=", "Full")})
				if in_flight:
					frappe.throw(_("Reduce cards only when all are Full ({0} in flight).").format(in_flight))
		self.recommended_cards = recommended_cards(self)

	def on_update(self):
		ensure_cards(self)
