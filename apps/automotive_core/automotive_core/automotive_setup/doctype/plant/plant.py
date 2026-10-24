# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

WAREHOUSE_FIELDS = (
	"receiving_warehouse",
	"inspection_warehouse",
	"raw_material_warehouse",
	"reserve_warehouse",
	"line_side_warehouse",
	"wip_warehouse",
	"quarantine_warehouse",
	"finished_goods_warehouse",
	"outbound_warehouse",
	"in_transit_warehouse",
	"scrap_warehouse",
	"consignment_warehouse",
)


class Plant(Document):
	def validate(self):
		self.plant_code = (self.plant_code or "").strip().upper()
		for field in WAREHOUSE_FIELDS:
			warehouse = self.get(field)
			if not warehouse:
				continue
			company, plant = frappe.db.get_value("Warehouse", warehouse, ["company", "automotive_plant"])
			if company != self.company:
				frappe.throw(
					_("{0}: warehouse {1} belongs to {2}, not {3}.").format(
						self.meta.get_label(field), warehouse, company, self.company
					)
				)
			if plant and plant != self.name:
				frappe.throw(
					_("{0}: warehouse {1} is assigned to plant {2}.").format(
						self.meta.get_label(field), warehouse, plant
					)
				)
		if (
			self.cost_center
			and frappe.db.get_value("Cost Center", self.cost_center, "company") != self.company
		):
			frappe.throw(_("Cost Center {0} does not belong to {1}.").format(self.cost_center, self.company))

	def on_update(self):
		# Warehouses chosen as plant defaults are scoped to this plant.
		for field in WAREHOUSE_FIELDS:
			warehouse = self.get(field)
			if warehouse and not frappe.db.get_value("Warehouse", warehouse, "automotive_plant"):
				frappe.db.set_value("Warehouse", warehouse, "automotive_plant", self.name)

	def default_warehouse(self, role: str) -> str | None:
		return self.get(f"{role}_warehouse")
