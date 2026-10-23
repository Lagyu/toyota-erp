"""Vehicle shipment, invoicing and acceptance states from sales documents (ERP-08, J-01).

Delivery Note  -> inventory Delivered, shipment Ready To Ship, allocation Delivered
Manifest       -> shipment Dispatched -> Delivered (proof of delivery)
Acceptance     -> shipment Accepted, warranty starts; damage raises a Transport Exception
Sales Invoice  -> invoicing Invoiced (credit note -> Credited); fully paid -> Paid
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_months, getdate, now_datetime

from automotive_core.governance import audit
from automotive_supply_chain.partners import assert_party_access


def _serials(row) -> list[str]:
	return [s.strip() for s in (row.get("serial_no") or "").split("\n") if s.strip()]


def vehicles_of(doc) -> list[str]:
	vins = []
	for row in doc.items:
		serials = _serials(row)
		if not serials and row.get("dn_detail"):
			serials = _serials(frappe.db.get_value("Delivery Note Item", row.dn_detail, ["serial_no"], as_dict=True) or {})
		if not serials and row.get("serial_and_batch_bundle"):
			from erpnext.stock.serial_batch_bundle import get_serial_nos

			serials = get_serial_nos(row.serial_and_batch_bundle)
		vins.extend(s for s in serials if frappe.db.exists("Vehicle Unit", s))
	return vins


def on_delivery_note_submit(doc, method=None) -> None:
	if doc.get("is_return"):
		for vin in vehicles_of(doc):
			frappe.db.set_value("Vehicle Unit", vin, {"inventory_status": "In Stock", "shipment_status": "Not Shipped",
				"current_warehouse": frappe.db.get_value("Serial No", vin, "warehouse")})
		return
	for vin in vehicles_of(doc):
		allocation = frappe.db.get_value("Vehicle Unit", vin, "automotive_allocation")
		if allocation:
			frappe.db.set_value("Vehicle Allocation", allocation, "status", "Delivered")
		frappe.db.set_value("Vehicle Unit", vin, {"inventory_status": "Delivered", "shipment_status": "Ready To Ship",
			"current_warehouse": None, "dealer": doc.customer})


def on_delivery_note_cancel(doc, method=None) -> None:
	for vin in vehicles_of(doc):
		allocation = frappe.db.get_value("Vehicle Unit", vin, "automotive_allocation")
		if allocation:
			frappe.db.set_value("Vehicle Allocation", allocation, "status", "Allocated")
		frappe.db.set_value("Vehicle Unit", vin, {"inventory_status": "Reserved" if allocation else "In Stock",
			"shipment_status": "Not Shipped", "current_warehouse": frappe.db.get_value("Serial No", vin, "warehouse")})


def on_sales_invoice_submit(doc, method=None) -> None:
	status = "Credited" if doc.get("is_return") else "Invoiced"
	for vin in vehicles_of(doc):
		frappe.db.set_value("Vehicle Unit", vin, "invoice_status", status)


def on_sales_invoice_cancel(doc, method=None) -> None:
	status = "Invoiced" if doc.get("is_return") else "Not Invoiced"
	for vin in vehicles_of(doc):
		frappe.db.set_value("Vehicle Unit", vin, "invoice_status", status)


def on_payment_entry_change(doc, method=None) -> None:
	if doc.party_type != "Customer":
		return
	for ref in doc.references:
		if ref.reference_doctype != "Sales Invoice":
			continue
		invoice = frappe.get_doc("Sales Invoice", ref.reference_name)
		status = "Paid" if invoice.outstanding_amount <= 0 and doc.docstatus == 1 else "Invoiced"
		for vin in vehicles_of(invoice):
			frappe.db.set_value("Vehicle Unit", vin, "invoice_status", status)


# ---------------------------------------------------------------------------------------
# Delivery acceptance
# ---------------------------------------------------------------------------------------


def validate_acceptance(doc) -> None:
	dn = frappe.db.get_value("Delivery Note", doc.delivery_note, ["customer", "docstatus", "is_return"], as_dict=True)
	if not dn or dn.docstatus != 1 or dn.is_return:
		frappe.throw(_("{0} is not a submitted delivery.").format(doc.delivery_note))
	doc.dealer = dn.customer
	assert_party_access("Customer", doc.dealer)
	if frappe.db.exists("Delivery Acceptance", {"delivery_note": doc.delivery_note, "docstatus": 1,
			"name": ("!=", doc.name)}):
		frappe.throw(_("Delivery {0} was already accepted.").format(doc.delivery_note))
	if doc.condition != "Accepted" and not (doc.damage_notes or "").strip():
		frappe.throw(_("Describe the damage or the reason for rejection."))
	doc.shipment_manifest = doc.shipment_manifest or frappe.db.get_value("Delivery Note", doc.delivery_note,
		"automotive_shipment_manifest")
	if doc.is_new():
		doc.accepted_at = now_datetime()


def on_acceptance_submit(doc) -> None:
	dn = frappe.get_doc("Delivery Note", doc.delivery_note)
	dn.db_set("automotive_acceptance_status", doc.condition)
	start = getdate(doc.accepted_at)
	for vin in vehicles_of(dn):
		model = frappe.db.get_value("Vehicle Unit", vin, "vehicle_model")
		months = int(frappe.get_cached_value("Vehicle Model", model, "warranty_months") or 36)
		values = {"shipment_status": "Accepted" if doc.condition != "Rejected" else "Delivered"}
		if doc.condition != "Rejected":
			values.update({"warranty_start_date": start, "warranty_end_date": add_months(start, months),
				"warranty_reference": f"WTY-{vin}"})
		frappe.db.set_value("Vehicle Unit", vin, values)
	if doc.condition != "Accepted":
		carrier = frappe.db.get_value("Shipment Manifest", doc.shipment_manifest, "carrier") if doc.shipment_manifest else None
		exc = frappe.get_doc({"doctype": "Transport Exception", "shipment_manifest": doc.shipment_manifest,
			"carrier": carrier, "exception_type": "Damage", "reported_at": now_datetime(),
			"serial_no": ", ".join(vehicles_of(dn))[:140], "description": doc.damage_notes, "status": "Open"})
		exc.flags.ignore_permissions = True
		exc.insert()
		doc.db_set("transport_exception", exc.name)
	audit.record("dealer.delivery_accepted", "Delivery Note", dn.name, {"acceptance": doc.name,
		"condition": doc.condition}, company=dn.company)
