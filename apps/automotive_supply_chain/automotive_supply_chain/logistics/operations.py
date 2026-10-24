"""Shipment manifests (proof of dispatch / delivery) and cycle counts (ERP-06)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from automotive_core.governance import audit
from automotive_core.governance.elevation import on_behalf_of
from automotive_core.integration_hub import outbox
from automotive_supply_chain.dealer_operations.fulfilment import vehicles_of

# ---------------------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------------------


def validate_manifest(doc) -> None:
	notes = []
	for row in doc.items:
		if row.delivery_note not in notes:
			notes.append(row.delivery_note)
	expanded = []
	for name in notes:
		dn = frappe.db.get_value("Delivery Note", name, ["docstatus", "company", "customer", "is_return",
			"automotive_shipment_manifest"], as_dict=True)
		if not dn or dn.docstatus != 1 or dn.is_return:
			frappe.throw(_("{0} is not a submitted outbound delivery.").format(name))
		if dn.company != doc.company:
			frappe.throw(_("{0} belongs to {1}.").format(name, dn.company))
		if dn.automotive_shipment_manifest and dn.automotive_shipment_manifest != doc.name:
			frappe.throw(_("{0} is already on manifest {1}.").format(name, dn.automotive_shipment_manifest))
		for item in frappe.get_all("Delivery Note Item", filters={"parent": name}, fields=["item_code", "qty",
				"serial_no"], order_by="idx asc"):
			expanded.append({"delivery_note": name, "customer": dn.customer, "item_code": item.item_code,
				"serial_no": (item.serial_no or "").replace("\n", ", ")[:140], "qty": item.qty, "line_status": "Loaded"})
	doc.set("items", expanded)
	if doc.transport_booking:
		capacity = frappe.db.get_value("Transport Booking", doc.transport_booking, "capacity_units")
		units = sum(flt(i["qty"]) for i in expanded)
		if capacity and units > capacity:
			frappe.throw(_("{0} units exceed the booked capacity of {1}.").format(units, capacity))


def on_manifest_submit(doc) -> None:
	doc.db_set({"delivery_status": "In Transit", "dispatched_by": frappe.session.user})
	for name in {r.delivery_note for r in doc.items}:
		frappe.db.set_value("Delivery Note", name, "automotive_shipment_manifest", doc.name)
		for vin in vehicles_of(frappe.get_doc("Delivery Note", name)):
			frappe.db.set_value("Vehicle Unit", vin, "shipment_status", "Dispatched")
	if doc.transport_booking:
		frappe.db.set_value("Transport Booking", doc.transport_booking, "status", "In Transit")
	audit.record("logistics.dispatched", doc.doctype, doc.name, {"carrier": doc.carrier,
		"deliveries": sorted({r.delivery_note for r in doc.items}), "seal": doc.seal_no}, company=doc.company)
	for customer in sorted({r.customer for r in doc.items}):
		outbox.publish("shipment.dispatched", {"manifest": doc.name, "departure": str(doc.departure),
			"vehicles": [r.serial_no for r in doc.items if r.customer == customer]}, doc.doctype, doc.name,
			party_type="Customer", party=customer, idempotency_key=f"shipment.dispatched:{doc.name}:{customer}")


@frappe.whitelist(methods=["POST"])
def record_delivery(name: str, received_by: str, delivered_at: str | None = None, exception: str | None = None) -> str:
	frappe.only_for(("Logistics Coordinator", "Warehouse Clerk", "System Manager"))
	doc = frappe.get_doc("Shipment Manifest", name)
	if doc.docstatus != 1 or doc.delivery_status != "In Transit":
		frappe.throw(_("Manifest {0} is {1}.").format(name, doc.delivery_status))
	status = "Exception" if exception else "Delivered"
	doc.db_set({"delivery_status": status, "delivered_at": delivered_at or now_datetime(),
		"received_by_name": received_by})
	for row in doc.items:
		row.db_set("line_status", "Exception" if exception else "Delivered")
	for dn in {r.delivery_note for r in doc.items}:
		for vin in vehicles_of(frappe.get_doc("Delivery Note", dn)):
			frappe.db.set_value("Vehicle Unit", vin, "shipment_status", "Delivered")
	if doc.transport_booking:
		frappe.db.set_value("Transport Booking", doc.transport_booking, "status", "Delivered")
	if exception:
		frappe.get_doc({"doctype": "Transport Exception", "shipment_manifest": doc.name, "carrier": doc.carrier,
			"exception_type": "Delay", "reported_at": now_datetime(), "description": exception,
			"status": "Open"}).insert(ignore_permissions=True)
	audit.record("logistics.delivered", doc.doctype, doc.name, {"received_by": received_by, "status": status},
		company=doc.company)
	return status


# ---------------------------------------------------------------------------------------
# Cycle counts
# ---------------------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def take_snapshot(name: str) -> int:
	doc = frappe.get_doc("Cycle Count", name)
	doc.check_permission("write")
	if doc.docstatus != 0:
		frappe.throw(_("The count is already posted."))
	rows = frappe.get_all("Bin", filters={"warehouse": doc.warehouse, "actual_qty": ("!=", 0)},
		fields=["item_code", "actual_qty"], order_by="item_code asc")
	existing = {(line.item_code, line.batch_no or "") for line in doc.lines}
	for row in rows:
		if (row.item_code, "") in existing:
			continue
		if frappe.get_cached_value("Item", row.item_code, "has_batch_no") or frappe.get_cached_value(
				"Item", row.item_code, "has_serial_no"):
			continue  # tracked items are counted by batch/serial through their own reconciliation
		doc.append("lines", {"item_code": row.item_code, "expected_qty": row.actual_qty})
	doc.save()
	return len(doc.lines)


def validate_count(doc) -> None:
	tolerance = flt(doc.tolerance_percent)
	counted = within = 0
	for line in doc.lines:
		if line.counted_qty is None:
			continue
		counted += 1
		line.variance_qty = flt(line.counted_qty) - flt(line.expected_qty)
		rate = flt(frappe.db.get_value("Bin", {"item_code": line.item_code, "warehouse": doc.warehouse},
			"valuation_rate"))
		line.variance_value = line.variance_qty * rate
		allowed = abs(flt(line.expected_qty)) * tolerance / 100
		line.within_tolerance = int(abs(line.variance_qty) <= allowed + 1e-9)
		within += line.within_tolerance
	doc.lines_counted, doc.lines_within_tolerance = counted, within
	doc.status = "Counted" if counted else "Draft"


def _reconciliation_row(doc, line, consignment: bool) -> dict:
	row = {"item_code": line.item_code, "warehouse": doc.warehouse, "qty": line.counted_qty,
		"valuation_rate": flt(frappe.db.get_value("Bin", {"item_code": line.item_code, "warehouse": doc.warehouse},
			"valuation_rate")) or None}
	if consignment:  # supplier-owned stock is carried at zero value
		row.update({"valuation_rate": 0, "allow_zero_valuation_rate": 1})
	if line.batch_no:  # the counted quantity of that batch in the warehouse
		row.update({"use_serial_batch_fields": 1, "batch_no": line.batch_no})
	return row


def on_count_submit(doc) -> None:
	variances = [ln for ln in doc.lines if abs(flt(ln.variance_qty)) > 1e-9]
	consignment = frappe.get_cached_value("Warehouse", doc.warehouse, "automotive_warehouse_role") == "Consignment"
	reco = None
	if variances:
		# the reviewed count is the authority: the reconciliation posts as a recorded system step
		# (counters do not hold Stock Reconciliation rights; ERPNext checks them while validating)
		with on_behalf_of(_("Stock reconciliation for count {0}").format(doc.name), doc.doctype, doc.name) as requester:
			recon = frappe.get_doc({"doctype": "Stock Reconciliation", "company": doc.company, "purpose":
				"Stock Reconciliation", "items": [_reconciliation_row(doc, ln, consignment) for ln in variances]})
			recon.insert()
			recon.submit()
			recon.db_set("owner", requester, update_modified=False)
		reco = recon.name
	doc.db_set({"status": "Posted", "stock_reconciliation": reco})
	audit.record("inventory.cycle_count_posted", doc.doctype, doc.name, {"warehouse": doc.warehouse,
		"counted": doc.lines_counted, "within_tolerance": doc.lines_within_tolerance, "reconciliation": reco},
		company=doc.company)
