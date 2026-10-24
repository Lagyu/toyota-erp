"""Vehicle Units: identity (VIN) and independent state machines (ERP-02 §2.2).

* Work Order submit (serialized vehicle or assembly item): one Vehicle Unit per unit,
  production status *Planned*, with a reserved VIN (check digit computed) that the
  Manufacture entry later uses as the item's Serial No.
* Manufacture Stock Entry submit: the units built by that entry become *Built*, in
  stock at the FG warehouse, quality *Pending Inspection*.
* Other modules move the other state machines: quality release (inspections, holds),
  inventory/shipment (allocation, dispatch), invoicing (sales invoices). No single
  field represents more than one of these states.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, getdate, now_datetime

from automotive_manufacturing.engine import vin as vin_engine

PLANT_VIN_CODES = {"AICHI-VA": "A", "KYUSHU-PT": "K", "NA-PDC": "N"}


def _model_for_item(item_code: str) -> str | None:
	item = frappe.get_cached_value(
		"Item", item_code, ["automotive_vehicle_model", "variant_of", "has_serial_no"], as_dict=True
	)
	if not item or not item.has_serial_no:
		return None
	model = item.automotive_vehicle_model
	if not model and item.variant_of:
		model = frappe.get_cached_value("Item", item.variant_of, "automotive_vehicle_model")
	return model


def _next_sequence(model: str, plant_code: str) -> int:
	"""Monotonic per model/plant/year, held in a row lock on the Vehicle Model."""
	frappe.db.sql("select name from `tabVehicle Model` where name=%s for update", model)
	prefix = frappe.get_cached_value("Vehicle Model", model, "vin_prefix")
	last = frappe.db.sql(
		"""select max(cast(substring(vin, 12, 6) as unsigned)) from `tabVehicle Unit`
		where vehicle_model=%s and substring(vin, 1, 8)=%s and substring(vin, 11, 1)=%s""",
		(model, prefix, plant_code),
	)[0][0]
	return cint(last) + 1


def reserve_units(work_order) -> list[str]:
	model = _model_for_item(work_order.production_item)
	if not model:
		return []
	prefix = frappe.get_cached_value("Vehicle Model", model, "vin_prefix")
	if not prefix:
		frappe.throw(_("Vehicle Model {0} has no VIN prefix.").format(model))
	plant = work_order.automotive_plant
	plant_code = PLANT_VIN_CODES.get(plant, "Z")
	year = getdate(work_order.planned_start_date or now_datetime()).year
	sequence = _next_sequence(model, plant_code)
	names = []
	for offset in range(cint(work_order.qty)):
		vin = vin_engine.build(prefix, year, plant_code, sequence + offset)
		unit = frappe.get_doc(
			{
				"doctype": "Vehicle Unit",
				"vin": vin,
				"vehicle_model": model,
				"variant_item": work_order.production_item,
				"configuration": work_order.get("automotive_configuration"),
				"company": work_order.company,
				"plant": plant,
				"production_line": work_order.get("automotive_production_line"),
				"work_order": work_order.name,
				"build_sequence": offset + 1,
				"sales_order": work_order.sales_order,
				"production_status": "Planned",
			}
		)
		unit.flags.ignore_permissions = True
		unit.insert()
		names.append(unit.name)
	return names


def on_work_order_submit(doc, method=None) -> None:
	reserve_units(doc)


def on_work_order_cancel(doc, method=None) -> None:
	for name in frappe.get_all(
		"Vehicle Unit", filters={"work_order": doc.name, "production_status": "Planned"}, pluck="name"
	):
		frappe.delete_doc("Vehicle Unit", name, ignore_permissions=True, force=True)


def units_for_entry(stock_entry) -> list[tuple[str, str]]:
	"""(item_code, serial_no) for every finished serialized unit produced by the entry."""
	out = []
	for row in stock_entry.items:
		if not (row.is_finished_item and row.t_warehouse):
			continue
		for serial in (row.serial_no or "").split("\n"):
			if serial.strip():
				out.append((row.item_code, serial.strip()))
		if not row.serial_no and row.serial_and_batch_bundle:
			from erpnext.stock.serial_batch_bundle import get_serial_nos

			out.extend((row.item_code, s) for s in get_serial_nos(row.serial_and_batch_bundle))
	return out


def planned_vins(work_order: str, qty: int) -> list[str]:
	"""The next ``qty`` reserved VINs of a work order, in build sequence."""
	return frappe.get_all(
		"Vehicle Unit",
		filters={"work_order": work_order, "production_status": "Planned"},
		pluck="name",
		order_by="build_sequence asc",
		limit=qty,
	)


def on_stock_entry_submit(doc, method=None) -> None:
	if doc.purpose != "Manufacture":
		return
	for _item_code, serial in units_for_entry(doc):
		if not frappe.db.exists("Vehicle Unit", serial):
			continue
		warehouse = frappe.db.get_value("Serial No", serial, "warehouse")
		frappe.db.set_value(
			"Vehicle Unit",
			serial,
			{
				"production_status": "Built",
				"built_at": now_datetime(),
				"serial_no": serial,
				"inventory_status": "In Stock",
				"current_warehouse": warehouse,
				"quality_status": "Pending Inspection",
			},
		)
		frappe.db.set_value("Serial No", serial, "automotive_vehicle_unit", serial, update_modified=False)


def on_stock_entry_cancel(doc, method=None) -> None:
	if doc.purpose != "Manufacture":
		return
	for _item, serial in units_for_entry(doc):
		if frappe.db.exists("Vehicle Unit", serial):
			frappe.db.set_value(
				"Vehicle Unit",
				serial,
				{
					"production_status": "Planned",
					"built_at": None,
					"inventory_status": "Not In Stock",
					"current_warehouse": None,
				},
			)


def set_state(vin: str, **states) -> None:
	"""Single entry point for other modules to move a unit's state machines."""
	allowed = {
		"production_status",
		"quality_status",
		"inventory_status",
		"shipment_status",
		"invoice_status",
		"current_warehouse",
		"final_inspection",
		"sales_order",
		"dealer",
		"warranty_start_date",
		"warranty_end_date",
		"warranty_reference",
	}
	unknown = set(states) - allowed
	if unknown:
		raise ValueError(f"unknown vehicle unit fields {unknown}")
	frappe.db.set_value("Vehicle Unit", vin, states)
