"""Tenant extension for supply chain: party groups, dealer price lists, settings and the
approval policies for procurement, dealer and intercompany documents."""

from __future__ import annotations

import frappe

from automotive_core.setup.tenant import approval_policy

POLICIES = [
	{"name": "Supplier Onboarding Approval", "doctype": "Supplier Onboarding",
	 "steps": [{"step": 1, "name": "Procurement manager", "role": "Procurement Manager"},
		{"step": 2, "name": "Quality manager", "role": "Quality Manager"}]},
	{"name": "Sourcing Award", "doctype": "Sourcing Decision",
	 "steps": [{"step": 1, "name": "Procurement manager", "role": "Procurement Manager"}]},
	{"name": "Dealer Onboarding", "doctype": "Dealer Registration", "amount_field": "requested_credit_limit",
	 "steps": [{"step": 1, "name": "Sales manager", "role": "Sales Manager"},
		{"step": 2, "name": "Credit (group controller)", "role": "Group Controller"}]},
	{"name": "Dealer Discount Approval", "doctype": "Sales Order", "amount_field": "automotive_max_discount_percent",
	 "min_amount": 3,
	 "steps": [{"step": 1, "name": "Sales manager", "role": "Sales Manager"},
		{"step": 2, "name": "Group controller", "role": "Group Controller", "min_amount": 8}]},
	{"name": "Transfer Price Approval", "doctype": "Transfer Price Agreement",
	 "steps": [{"step": 1, "name": "Group controller", "role": "Group Controller"}]},
]

SETTINGS = {"qty_tolerance_percent": 0, "price_tolerance_percent": 2, "hold_payment_on_exception": 1,
	"schedule_firm_days": 10, "schedule_horizon_days": 56, "discount_approval_percent": 3,
	"promise_transit_days": 7, "auto_allocate": 1}


def _ensure(doctype: str, name_field: str, name: str, values: dict, log) -> None:
	if frappe.db.exists(doctype, name):
		return
	if log.change(f"{doctype} {name}"):
		frappe.get_doc({"doctype": doctype, name_field: name, **values}).insert(ignore_permissions=True)


def apply(definition, log) -> None:
	_ensure("Supplier Group", "supplier_group_name", "Carriers",
		{"parent_supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 1}, "name")}, log)
	_ensure("Supplier Group", "supplier_group_name", "Production Material",
		{"parent_supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 1}, "name")}, log)
	_ensure("Customer Group", "customer_group_name", "Dealers",
		{"parent_customer_group": frappe.db.get_value("Customer Group", {"is_group": 1}, "name")}, log)
	for name, currency in (("Dealer Price JP", "JPY"), ("Dealer Price US", "USD")):
		_ensure("Price List", "price_list_name", name, {"currency": currency, "selling": 1, "enabled": 1}, log)
	settings = frappe.get_single("Supply Chain Settings")
	changes = {k: v for k, v in SETTINGS.items() if settings.get(k) in (None, "")}
	if changes and log.change(f"Supply Chain Settings: {sorted(changes)}"):
		settings.update(changes)
		settings.flags.ignore_permissions = True
		settings.save()
	for policy in POLICIES:
		approval_policy(policy, log)
	# Purchase order policies must not catch blanket-order call-offs (already approved spend).
	for name in frappe.get_all("Approval Policy", filters={"reference_doctype": "Purchase Order",
			"category_field": ("is", "not set")}, pluck="name"):
		if log.change(f"approval policy {name}: exclude blanket call-offs"):
			frappe.db.set_value("Approval Policy", name, {"category_field": "automotive_release_type",
				"category_values": "Standard"})
