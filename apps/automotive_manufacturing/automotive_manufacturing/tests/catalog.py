"""Small product catalogue for manufacturing integration tests (test site only).

H1T test vehicle (variants by Trim x Drivetrain), a batch-tracked seat sub-assembly
made from batch-tracked foam, batch-tracked wheels and serialized ECUs, all built at
AICHI-VA on line AICHI-FA1. Everything goes through ERPNext documents.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, nowdate

from automotive_core.tests.utils import AICHI

PLANT = "AICHI-VA"
LINE = "AICHI-FA1"
ABBR = "HAMJ"
LINE_SIDE = "Aichi Final Assembly 1 (H1 / H2) Line Side - HAMJ"
PARTS_STORE = "Aichi Parts Store - HAMJ"
FG = "Aichi Finished Vehicle Yard - HAMJ"
WIP = "Aichi Body & Paint WIP - HAMJ"
QUARANTINE = "Aichi Quarantine - HAMJ"
TEMPLATE = "H1T"
MODEL = "H1T"


def _attribute(name: str, values: list[tuple[str, str]]) -> None:
	if frappe.db.exists("Item Attribute", name):
		return
	frappe.get_doc({"doctype": "Item Attribute", "attribute_name": name,
		"item_attribute_values": [{"attribute_value": v, "abbr": a} for v, a in values]}).insert(ignore_permissions=True)


def _item(code: str, group: str, **extra) -> str:
	if frappe.db.exists("Item", code):
		return code
	frappe.get_doc({
		"doctype": "Item",
		"item_code": code,
		"item_name": extra.pop("item_name", code),
		"item_group": group,
		"stock_uom": extra.pop("stock_uom", "Nos"),
		"is_stock_item": 1,
		"valuation_rate": extra.pop("valuation_rate", 100),
		"item_defaults": [{"company": AICHI, "default_warehouse": extra.pop("default_warehouse", PARTS_STORE)}],
		**extra,
	}).insert(ignore_permissions=True)
	return code


def _supplier(name: str) -> str:
	if not frappe.db.exists("Supplier", name):
		frappe.get_doc({"doctype": "Supplier", "supplier_name": name,
			"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")}).insert(ignore_permissions=True)
	return name


def _bom(item: str, lines: list[tuple[str, float]], **extra) -> str:
	existing = frappe.db.get_value("BOM", {"item": item, "docstatus": 1, "is_active": 1}, "name")
	if existing:
		return existing
	bom = frappe.get_doc({
		"doctype": "BOM",
		"item": item,
		"company": AICHI,
		"quantity": 1,
		"is_default": 1,
		"rm_cost_as_per": "Valuation Rate",
		"items": [{"item_code": code, "qty": qty, "automotive_net_qty": qty} for code, qty in lines],
		**extra,
	})
	bom.insert(ignore_permissions=True)
	bom.submit()
	return bom.name


def build() -> dict:
	_attribute("Trim", [("Base", "B"), ("Premium", "P")])
	_attribute("Drivetrain", [("FWD", "F"), ("AWD", "A")])
	_supplier("Shiranui Foam Industries")
	_supplier("Kaminari Electronics")
	foam = _item("TST-FOAM", "Raw Material", has_batch_no=1, create_new_batch=1, batch_number_series="TFOAM-.#####",
		is_purchase_item=1, valuation_rate=40, lead_time_days=5, stock_uom="Kg")
	wheel = _item("TST-WHEEL", "Raw Material", has_batch_no=1, create_new_batch=1, batch_number_series="TWHL-.#####",
		is_purchase_item=1, valuation_rate=120, lead_time_days=3)
	ecu = _item("TST-ECU", "Raw Material", has_serial_no=1, serial_no_series="TECU-.#####", is_purchase_item=1,
		valuation_rate=900, lead_time_days=10)
	seat = _item("TST-SEAT", "Sub Assemblies", has_batch_no=1, create_new_batch=1, batch_number_series="TSEAT-.#####",
		is_purchase_item=0, valuation_rate=300, lead_time_days=1, default_warehouse=LINE_SIDE)
	if not frappe.db.exists("Item", TEMPLATE):
		frappe.get_doc({
			"doctype": "Item", "item_code": TEMPLATE, "item_name": "H1 Test Vehicle", "item_group": "Products",
			"stock_uom": "Nos", "is_stock_item": 1, "has_variants": 1, "has_serial_no": 1,
			"attributes": [{"attribute": "Trim"}, {"attribute": "Drivetrain"}],
			"item_defaults": [{"company": AICHI, "default_warehouse": FG}],
		}).insert(ignore_permissions=True)
	if not frappe.db.exists("Vehicle Model", MODEL):
		frappe.get_doc({
			"doctype": "Vehicle Model", "model_code": MODEL, "model_name": "H1 Test", "product_family": "Passenger Car",
			"template_item": TEMPLATE, "default_plant": PLANT, "production_line": LINE, "vin_prefix": "JX9H1T0E",
			"option_groups": [{"attribute": "Trim", "required": 1, "display_order": 1},
				{"attribute": "Drivetrain", "required": 1, "display_order": 2}],
		}).insert(ignore_permissions=True)
		frappe.db.set_value("Item", TEMPLATE, "automotive_vehicle_model", MODEL)
	if not frappe.db.exists("Vehicle Option Rule", "H1T-BASE-FWD"):
		frappe.get_doc({"doctype": "Vehicle Option Rule", "rule_code": "H1T-BASE-FWD", "vehicle_model": MODEL,
			"rule_type": "Excludes", "if_attribute": "Trim", "if_values": "Base", "then_attribute": "Drivetrain",
			"then_values": "AWD", "message": "AWD needs the Premium trim"}).insert(ignore_permissions=True)
	from automotive_manufacturing.product_engineering.configuration import resolve_variant

	variants = {
		("Premium", "AWD"): resolve_variant(MODEL, {"Trim": "Premium", "Drivetrain": "AWD"}),
		("Base", "FWD"): resolve_variant(MODEL, {"Trim": "Base", "Drivetrain": "FWD"}),
	}
	seat_bom = _bom(seat, [(foam, 1.5)])
	vehicle_boms = {key: _bom(code, [(seat, 2), (wheel, 4), (ecu, 1)]) for key, code in variants.items()}
	return {"foam": foam, "wheel": wheel, "ecu": ecu, "seat": seat, "variants": variants, "seat_bom": seat_bom,
		"vehicle_boms": vehicle_boms}


def receive(item_code: str, qty: float, warehouse: str = LINE_SIDE, rate: float = 100, batch_no: str | None = None,
		serial_nos: list[str] | None = None, supplier: str | None = None) -> str:
	se = frappe.get_doc({
		"doctype": "Stock Entry",
		"purpose": "Material Receipt",
		"stock_entry_type": "Material Receipt",
		"company": AICHI,
		"items": [{"item_code": item_code, "qty": qty, "t_warehouse": warehouse, "basic_rate": rate,
			"use_serial_batch_fields": 1, "batch_no": batch_no, "serial_no": "\n".join(serial_nos or [])}],
	})
	se.insert(ignore_permissions=True)
	se.submit()
	if supplier:
		for row in se.items:
			if row.batch_no:
				frappe.db.set_value("Batch", row.batch_no, "automotive_supplier", supplier)
	return se.name


def make_batch(item: str, batch_id: str, supplier: str | None = None) -> str:
	if not frappe.db.exists("Batch", batch_id):
		frappe.get_doc({"doctype": "Batch", "batch_id": batch_id, "item": item, "automotive_supplier": supplier}).insert(
			ignore_permissions=True)
	return batch_id


def work_order(item: str, qty: int, bom: str | None = None, start_offset_days: int = 0, submit: bool = True,
		fg_warehouse: str = FG, pinned: bool = False, sales_order: str | None = None,
		sales_order_item: str | None = None):
	wo = frappe.get_doc({
		"doctype": "Work Order",
		"production_item": item,
		"bom_no": bom,
		"automotive_bom_pinned": int(pinned),
		"qty": qty,
		"company": AICHI,
		"planned_start_date": f"{add_days(nowdate(), start_offset_days)} 08:00:00",
		"source_warehouse": LINE_SIDE,
		"wip_warehouse": WIP,
		"fg_warehouse": fg_warehouse,
		"skip_transfer": 1,
		"use_multi_level_bom": 0,
		"automotive_production_line": LINE,
		"automotive_plant": PLANT,
		"sales_order": sales_order,
		"sales_order_item": sales_order_item,
	})
	wo.insert(ignore_permissions=True)
	if submit:
		wo.submit()
	return wo
