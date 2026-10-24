"""Least-privilege grants for persona roles on standard ERPNext DocTypes (ERP-10).

Persona roles get exactly the rights listed here on standard DocTypes; everything
else comes from the ERPNext roles bundled in their Role Profile (setup/roles.py).
Grants are written as Custom DocPerm rows (the supported Role Permission Manager
mechanism) and re-applied idempotently on every migrate.

Field-level restrictions use permlevel property setters: supplier bank account
numbers are permlevel 1 and readable only by finance roles.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.permissions import add_permission, update_permission_property

READ = ("read", "report", "print")
WRITE = ("read", "write", "create", "report", "print")
SUBMIT = ("read", "write", "create", "submit", "report", "print")

GRANTS: list[tuple[str, str, tuple[str, ...]]] = [
	# Line operators: execute job cards and record downtime; read context only.
	("Job Card", "Line Operator", ("read", "write", "report", "print")),
	("Work Order", "Line Operator", READ),
	("Workstation", "Line Operator", READ),
	("Operation", "Line Operator", READ),
	("Item", "Line Operator", READ),
	("Warehouse", "Line Operator", READ),
	("Batch", "Line Operator", READ),
	("Serial No", "Line Operator", READ),
	("Downtime Entry", "Line Operator", WRITE),
	# Supervisors approve operator work and record downtime / handovers; posting a build of a
	# batch-tracked module creates its production batch.
	("Downtime Entry", "Production Supervisor", SUBMIT),
	("Batch", "Production Supervisor", WRITE),
	# Quality inspectors: inspections and nonconformance capture.
	("Quality Inspection", "Quality Inspector", SUBMIT),
	("Quality Inspection Template", "Quality Inspector", READ),
	("Non Conformance", "Quality Inspector", WRITE),
	("Item", "Quality Inspector", READ),
	("Batch", "Quality Inspector", READ),
	("Serial No", "Quality Inspector", READ),
	("Purchase Receipt", "Quality Inspector", READ),
	("Stock Entry", "Quality Inspector", READ),
	("Work Order", "Quality Inspector", READ),
	("Job Card", "Quality Inspector", READ),
	("Delivery Note", "Quality Inspector", READ),
	("Warehouse", "Quality Inspector", READ),
	("Supplier", "Quality Inspector", READ),
	("Quality Inspection Template", "Quality Manager", WRITE),
	# Dealer support: requests raised in the dealer portal are worked by the dealer liaisons.
	("Issue", "Dealer Liaison", WRITE),
	# Product engineering: items, BOMs and routings (approval through ECO).
	("Item", "Product Engineer", WRITE),
	("Item Attribute", "Product Engineer", WRITE),
	("Item Alternative", "Product Engineer", WRITE),
	("BOM", "Product Engineer", WRITE),
	("Routing", "Product Engineer", WRITE),
	("Operation", "Product Engineer", WRITE),
	("Workstation", "Product Engineer", READ),
	("Work Order", "Product Engineer", READ),
	("Supplier", "Product Engineer", READ),
	("Item", "Engineering Manager", WRITE),
	("BOM", "Engineering Manager", (*SUBMIT, "cancel", "amend")),
	# Treasury approvers review payments; they never create or submit them.
	("Payment Entry", "Treasury Approver", READ),
	("Purchase Invoice", "Treasury Approver", READ),
	("Supplier", "Treasury Approver", READ),
	("Bank Account", "Treasury Approver", READ),
	("Bank", "Treasury Approver", READ),
	("Bank Account", "AP Clerk", WRITE),
	# AP books import costs (freight, duty) into the received goods' valuation
	("Landed Cost Voucher", "AP Clerk", SUBMIT),
	# Procurement: blanket (scheduling) agreements are negotiated by buyers and approved by
	# procurement managers; ERPNext itself grants purchase roles only "select" on them.
	("Blanket Order", "Buyer", SUBMIT),
	("Blanket Order", "Procurement Manager", (*SUBMIT, "cancel", "amend")),
	("Blanket Order", "Production Planner", READ),
	("Blanket Order", "AP Clerk", READ),
	# Buyers issue RFQs (ERPNext reserves submit for Purchase Manager).
	("Request for Quotation", "Buyer", SUBMIT),
	("Request for Quotation", "Procurement Manager", (*SUBMIT, "cancel", "amend")),
	# Receiving: ERPNext validates the supplier's payable account currency on every receipt,
	# which needs "select" on Account (ERPNext grants it to Stock Manager but not Stock User).
	("Account", "Warehouse Clerk", ("select",)),
	# receiving registers supplier lots as batches
	("Batch", "Warehouse Clerk", WRITE),
	# Logistics: shipments and dispatch.
	("Shipment", "Logistics Coordinator", SUBMIT),
	("Delivery Note", "Logistics Coordinator", READ),
	# Executives: read-only management visibility.
	*[
		(doctype, "Automotive Executive", READ)
		for doctype in (
			"Sales Order",
			"Sales Invoice",
			"Purchase Order",
			"Purchase Invoice",
			"Work Order",
			"Stock Entry",
			"Item",
			"Customer",
			"Supplier",
			"Warehouse",
			"Company",
			"GL Entry",
			"Quality Inspection",
		)
	],
	# Auditors: read across operations (ERPNext's Auditor covers accounting).
	*[
		(doctype, "Auditor", READ)
		for doctype in (
			"Stock Entry",
			"Purchase Receipt",
			"Delivery Note",
			"Work Order",
			"Job Card",
			"Quality Inspection",
			"BOM",
			"Purchase Order",
			"Sales Order",
			"Material Request",
			"Item",
			"Warehouse",
			"Stock Reconciliation",
			"Landed Cost Voucher",
			"Non Conformance",
			"Blanket Order",
			"Stock Reservation Entry",
			"Serial No",
			"Batch",
			"Serial and Batch Bundle",
			"Stock Ledger Entry",
			"Bin",
			# ERPNext's Auditor reads purchase invoices and journals but not these
			"Sales Invoice",
			"Payment Entry",
		)
	],
]

# (doctype, fieldname): restricted to permlevel 1
RESTRICTED_FIELDS = [
	("Bank Account", "bank_account_no"),
	("Bank Account", "iban"),
	("Bank Account", "branch_code"),
]
PERMLEVEL_1 = [
	("Bank Account", "Treasury Approver", ("read",)),
	("Bank Account", "Group Controller", ("read",)),
	("Bank Account", "Accounts Manager", ("read", "write")),
	("Bank Account", "AP Clerk", ("read", "write")),
	("Bank Account", "System Manager", ("read", "write")),
]


def _grant(doctype: str, role: str, ptypes: tuple[str, ...], permlevel: int = 0) -> None:
	if not frappe.db.exists("DocType", doctype) or not frappe.db.exists("Role", role):
		return
	exists = frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": role, "permlevel": permlevel})
	if not exists:
		add_permission(doctype, role, permlevel)
	for ptype in ptypes:
		update_permission_property(doctype, role, permlevel, ptype, 1, validate=False)


def apply() -> None:
	for doctype, role, ptypes in GRANTS:
		_grant(doctype, role, ptypes)
	for doctype, fieldname in RESTRICTED_FIELDS:
		if frappe.get_meta(doctype).has_field(fieldname):
			make_property_setter(doctype, fieldname, "permlevel", 1, "Int", validate_fields_for_doctype=False)
	for doctype, role, ptypes in PERMLEVEL_1:
		_grant(doctype, role, ptypes, permlevel=1)
	frappe.clear_cache()
