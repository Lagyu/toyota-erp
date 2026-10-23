"""Recall impact analysis (J-08): suspect lots/serials -> every affected unit, with a
reproducible query hash and result hash."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime

from automotive_core.governance import audit
from automotive_core.utils import canonical_json, sha256_hex
from automotive_manufacturing.traceability.genealogy import units_containing


def _lines(text: str | None) -> list[str]:
	return sorted({v.strip() for v in (text or "").splitlines() if v.strip()})


def analyze(doc) -> None:
	query = {
		"component_item": doc.component_item,
		"batch_nos": _lines(doc.batch_nos),
		"serial_nos": _lines(doc.serial_nos),
		"supplier": doc.supplier,
		"built_from": str(doc.built_from or ""),
		"built_to": str(doc.built_to or ""),
	}
	if not (query["batch_nos"] or query["serial_nos"] or query["supplier"]):
		frappe.throw(_("Give suspect batches, serials or a supplier."))
	units = units_containing(doc.component_item, query["batch_nos"], query["serial_nos"], doc.supplier)
	rows = []
	for unit in units:
		vehicle = frappe.db.get_value("Vehicle Unit", unit["serial_no"],
			["name", "built_at", "inventory_status", "shipment_status", "current_warehouse", "dealer"], as_dict=True)
		built = getdate(vehicle.built_at) if vehicle and vehicle.built_at else None
		if doc.built_from and built and built < getdate(doc.built_from):
			continue
		if doc.built_to and built and built > getdate(doc.built_to):
			continue
		if vehicle and vehicle.inventory_status == "Delivered":
			location = _("Delivered ({0})").format(vehicle.shipment_status)
		elif vehicle:
			location = vehicle.current_warehouse or vehicle.inventory_status
		else:
			location = frappe.db.get_value("Serial No", unit["serial_no"], "warehouse") or ""
		rows.append({
			"unit_serial_no": unit["serial_no"],
			"unit_item": unit["item_code"],
			"via": unit["path"][:140],
			"vehicle_unit": vehicle.name if vehicle else None,
			"location": location,
			"dealer": vehicle.dealer if vehicle else None,
		})
	doc.set("affected", rows)
	doc.affected_units = len(rows)
	doc.affected_at_dealers = sum(1 for r in rows if r["dealer"] and r["location"].startswith(_("Delivered")))
	doc.affected_in_stock = len(rows) - doc.affected_at_dealers
	doc.query_sha256 = sha256_hex(canonical_json(query))
	doc.result_sha256 = sha256_hex(canonical_json([r["unit_serial_no"] for r in rows]))
	doc.analyzed_at = now_datetime()
	doc.status = "Analyzed"


@frappe.whitelist(methods=["POST"])
def run_analysis(name: str) -> dict:
	doc = frappe.get_doc("Recall Campaign", name)
	doc.check_permission("write")
	analyze(doc)
	doc.save()
	audit.record("recall.analyzed", doc.doctype, doc.name,
		{"affected": doc.affected_units, "query": doc.query_sha256, "result": doc.result_sha256})
	return {"affected_units": doc.affected_units, "result_sha256": doc.result_sha256}
