# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class ProductionLine(Document):
	def validate(self):
		self.line_code = (self.line_code or "").strip().upper()
		if flt(self.takt_time_seconds) <= 0:
			frappe.throw(_("Takt time must be greater than zero."))
		self.planned_rate_per_hour = flt(3600.0 / flt(self.takt_time_seconds), 3)
		minutes = flt(frappe.db.get_value("Shift Pattern", self.shift_pattern, "available_minutes_per_day"))
		self.max_units_per_day = int(minutes * 60 // flt(self.takt_time_seconds))
		for field in ("line_side_warehouse", "wip_warehouse"):
			warehouse = self.get(field)
			if warehouse:
				plant = frappe.db.get_value("Warehouse", warehouse, "automotive_plant")
				if plant and plant != self.plant:
					frappe.throw(_("{0} {1} belongs to plant {2}.").format(self.meta.get_label(field), warehouse, plant))
		seen_codes, seen_sequence = set(), set()
		for station in self.stations:
			if station.station_code in seen_codes:
				frappe.throw(_("Station {0} appears twice.").format(station.station_code))
			if station.sequence in seen_sequence:
				frappe.throw(_("Two stations share sequence {0}.").format(station.sequence))
			seen_codes.add(station.station_code)
			seen_sequence.add(station.sequence)
			ws_plant = frappe.db.get_value("Workstation", station.workstation, "automotive_plant")
			if ws_plant and ws_plant != self.plant:
				frappe.throw(_("Workstation {0} belongs to plant {1}.").format(station.workstation, ws_plant))
		self.stations.sort(key=lambda s: s.sequence)
		for idx, station in enumerate(self.stations, start=1):
			station.idx = idx

	def on_update(self):
		for station in self.stations:
			frappe.db.set_value("Workstation", station.workstation,
				{"automotive_plant": self.plant, "automotive_production_line": self.name})
		if self.default_workstation:
			frappe.db.set_value("Workstation", self.default_workstation,
				{"automotive_plant": self.plant, "automotive_production_line": self.name})

	def builds(self, item_code: str) -> bool:
		"""True when the line is configured to build this item (directly, via template or item group)."""
		if not self.products:
			return True
		item = frappe.get_cached_value("Item", item_code, ["variant_of", "item_group"], as_dict=True) or {}
		for product in self.products:
			if product.item and product.item in (item_code, item.get("variant_of")):
				return True
			if product.item_group and product.item_group == item.get("item_group"):
				return True
		return False
