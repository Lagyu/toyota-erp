"""Dealer portal API (ERP-08): configuration, orders, changes, allocated inventory,
shipments and acceptance, invoices and support requests — for the caller's own
dealership only (party resolved from Partner User Access on the server)."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt

from automotive_core.integration_hub.idempotency import idempotent
from automotive_supply_chain.partners import capabilities, require_capability, require_party

PAGE = 50


def _dealer(capability: str | None = None) -> str:
	"""The caller's own customer (and, with ``capability``, that their portal role allows it)."""
	return require_capability("Customer", capability) if capability else require_party("Customer")


def _owned(doctype: str, name: str, dealer: str, field: str):
	if frappe.db.get_value(doctype, name, field) != dealer:
		raise frappe.PermissionError(_("Not found."))
	return frappe.get_doc(doctype, name)


@frappe.whitelist()
def dashboard() -> dict[str, Any]:
	"""Counters for the overview, only those the caller's portal role may see."""
	dealer = _dealer()
	allowed = capabilities("Customer")
	counters = {
		"open_orders": (
			"orders",
			lambda: frappe.db.count(
				"Dealer Order", {"dealer": dealer, "status": ("in", ("Submitted", "Accepted"))}
			),
		),
		"allocated_vehicles": (
			"inventory",
			lambda: frappe.db.count("Vehicle Unit", {"dealer": dealer, "inventory_status": "Reserved"}),
		),
		"in_delivery": (
			"deliveries",
			lambda: frappe.db.count(
				"Vehicle Unit",
				{"dealer": dealer, "shipment_status": ("in", ("Ready To Ship", "Dispatched", "Delivered"))},
			),
		),
		"open_invoices": (
			"invoices",
			lambda: frappe.db.count(
				"Sales Invoice", {"customer": dealer, "docstatus": 1, "outstanding_amount": (">", 0)}
			),
		),
		"open_support_requests": (
			"support",
			lambda: frappe.db.count("Issue", {"customer": dealer, "status": ("in", ("Open", "Replied"))}),
		),
	}
	return {
		"dealer": dealer,
		"capabilities": allowed,
		**{key: count() for key, (capability, count) in counters.items() if capability in allowed},
	}


@frappe.whitelist()
def models() -> list[dict]:
	dealer = _dealer("catalogue")
	registration = frappe.db.get_value("Customer", dealer, "automotive_dealer_registration")
	allowed = (
		set(
			frappe.get_all(
				"Dealer Authorised Model",
				filters={"parent": registration},
				pluck="vehicle_model",
				ignore_permissions=True,
			)
		)
		if registration
		else set()
	)
	out = []
	orderable = {"lifecycle_status": ("in", ("Active", "Run-out"))}
	for model in frappe.get_all(
		"Vehicle Model",
		filters=orderable,
		fields=["name", "model_name", "product_family"],
		ignore_permissions=True,
	):
		if allowed and model.name not in allowed:
			continue
		groups = frappe.get_all(
			"Vehicle Model Option Group",
			filters={"parent": model.name},
			fields=["attribute", "required", "display_order"],
			order_by="display_order asc",
			ignore_permissions=True,
		)
		for group in groups:
			group["values"] = frappe.get_all(
				"Item Attribute Value",
				filters={"parent": group["attribute"]},
				pluck="attribute_value",
				order_by="idx asc",
				ignore_permissions=True,
			)
		out.append({**model, "options": groups})
	return out


def _registration(dealer: str) -> frappe._dict:
	name = frappe.db.get_value("Customer", dealer, "automotive_dealer_registration")
	return (
		frappe.db.get_value("Dealer Registration", name, ["name", "company"], as_dict=True)
		if name
		else frappe._dict()
	)


@frappe.whitelist()
def check_configuration(
	vehicle_model: str, selections: str | dict, qty: int = 1, requested_delivery_date: str | None = None
) -> dict:
	"""Validate a configuration and, when valid, quote the dealer price and a promise date.
	No document is created."""
	from automotive_manufacturing.product_engineering.configuration import check_selections, find_variant
	from automotive_supply_chain.dealer_operations.orders import _price, promise_date

	dealer = _dealer("catalogue")
	if isinstance(selections, str):
		selections = json.loads(selections)
	selections = {k: str(v) for k, v in selections.items() if v not in (None, "")}
	result = check_selections(vehicle_model, selections)
	out = {"valid": result["valid"], "violations": result["violations"]}
	if not result["valid"]:
		return out
	template = frappe.db.get_value("Vehicle Model", vehicle_model, "template_item")
	variant = find_variant(template, selections)
	if not variant:
		out.update(
			valid=False,
			violations=[
				{"rule": "not-buildable", "attribute": None, "message": _("This combination is not offered.")}
			],
		)
		return out
	price_list = frappe.db.get_value("Customer", dealer, "default_price_list")
	company = _registration(dealer).company
	out.update(
		{
			"variant": variant,
			"list_price": _price(variant, price_list),
			"currency": frappe.db.get_value("Price List", price_list, "currency"),
			"promise_date": promise_date(variant, max(1, int(qty or 1)), company, requested_delivery_date),
		}
	)
	return out


@frappe.whitelist(methods=["POST"])
@idempotent("portal.dealer.place_order")
def place_order(
	dealer_reference: str, requested_delivery_date: str, lines: str | list, order_type: str = "Customer Order"
) -> dict:
	"""The selling company is the dealer's contracting company (from the registration)."""
	from automotive_manufacturing.product_engineering.configuration import configure

	dealer = _dealer("order")
	rows = json.loads(lines) if isinstance(lines, str) else lines
	if not rows:
		frappe.throw(_("Add at least one vehicle."))
	items = []
	for row in rows:
		cfg = configure(row["vehicle_model"], row["selections"])
		items.append(
			{
				"vehicle_model": row["vehicle_model"],
				"configuration": cfg["configuration"],
				"qty": int(row.get("qty") or 1),
			}
		)
	doc = frappe.get_doc(
		{
			"doctype": "Dealer Order",
			"dealer": dealer,
			"dealer_reference": dealer_reference,
			"company": _registration(dealer).company,
			"order_type": order_type,
			"requested_delivery_date": requested_delivery_date,
			"items": items,
		}
	)
	doc.insert()
	doc.submit()
	return {"dealer_order": doc.name, "status": doc.status, "total": doc.total, "currency": doc.currency}


@frappe.whitelist()
def orders(page: int = 0) -> list[dict]:
	return frappe.get_all(
		"Dealer Order",
		filters={"dealer": _dealer("orders")},
		fields=[
			"name",
			"dealer_reference",
			"order_type",
			"status",
			"requested_delivery_date",
			"promised_date",
			"total",
			"currency",
			"creation",
		],
		order_by="creation desc",
		limit_start=int(page) * PAGE,
		limit_page_length=PAGE,
		ignore_permissions=True,
	)


@frappe.whitelist()
def order(name: str) -> dict:
	dealer = _dealer("orders")
	doc = _owned("Dealer Order", name, dealer, "dealer")
	vehicles = []
	if doc.sales_order:
		vehicles = frappe.get_all(
			"Vehicle Unit",
			filters={"sales_order": doc.sales_order},
			fields=[
				"name",
				"variant_item",
				"production_status",
				"quality_status",
				"inventory_status",
				"shipment_status",
			],
			ignore_permissions=True,
		)
	return {
		"name": doc.name,
		"reference": doc.dealer_reference,
		"status": doc.status,
		"promised_date": str(doc.promised_date or ""),
		"currency": doc.currency,
		"total": doc.total,
		"lines": [
			{
				"idx": i.idx,
				"model": i.vehicle_model,
				"variant": i.variant_item,
				"qty": i.qty,
				"amount": i.amount,
			}
			for i in doc.items
		],
		"vehicles": vehicles,
		"changes": frappe.get_all(
			"Dealer Order Change",
			filters={"dealer_order": doc.name},
			fields=["name", "change_type", "status", "effect"],
			ignore_permissions=True,
		),
	}


@frappe.whitelist(methods=["POST"])
@idempotent("portal.dealer.request_change")
def request_change(
	dealer_order: str,
	change_type: str,
	reason: str,
	item_row: int = 1,
	new_qty: int | None = None,
	new_delivery_date: str | None = None,
	new_selections: str | dict | None = None,
) -> dict:
	from automotive_manufacturing.product_engineering.configuration import configure

	dealer = _dealer("order")
	order_doc = _owned("Dealer Order", dealer_order, dealer, "dealer")
	values = {
		"doctype": "Dealer Order Change",
		"dealer_order": dealer_order,
		"change_type": change_type,
		"item_row": item_row,
		"new_qty": new_qty,
		"new_delivery_date": new_delivery_date,
		"reason": reason,
	}
	if new_selections:
		selections = json.loads(new_selections) if isinstance(new_selections, str) else new_selections
		model = order_doc.items[int(item_row) - 1].vehicle_model
		values["new_configuration"] = configure(model, selections)["configuration"]
	doc = frappe.get_doc(values)
	doc.insert()
	doc.submit()
	return {"change": doc.name, "status": doc.status, "effect": doc.effect}


@frappe.whitelist()
def inventory() -> list[dict]:
	return frappe.get_all(
		"Vehicle Unit",
		filters={"dealer": _dealer("inventory")},
		fields=[
			"name",
			"variant_item",
			"inventory_status",
			"shipment_status",
			"invoice_status",
			"sales_order",
			"warranty_start_date",
		],
		order_by="modified desc",
		limit_page_length=200,
		ignore_permissions=True,
	)


@frappe.whitelist()
def deliveries() -> list[dict]:
	dealer = _dealer("deliveries")
	notes = frappe.get_all(
		"Delivery Note",
		filters={"customer": dealer, "docstatus": 1, "is_return": 0},
		fields=["name", "posting_date", "automotive_shipment_manifest", "automotive_acceptance_status"],
		order_by="posting_date desc",
		limit_page_length=PAGE,
		ignore_permissions=True,
	)
	for dn in notes:
		dn["vehicles"] = [
			s
			for row in frappe.get_all(
				"Delivery Note Item",
				filters={"parent": dn["name"]},
				pluck="serial_no",
				ignore_permissions=True,
			)
			for s in (row or "").split("\n")
			if s
		]
		if dn.get("automotive_shipment_manifest"):
			dn["delivery_status"] = frappe.db.get_value(
				"Shipment Manifest", dn["automotive_shipment_manifest"], "delivery_status"
			)
	return notes


@frappe.whitelist(methods=["POST"])
@idempotent("portal.dealer.accept_delivery")
def accept_delivery(delivery_note: str, condition: str, damage_notes: str | None = None) -> dict:
	_owned("Delivery Note", delivery_note, _dealer("accept_delivery"), "customer")
	doc = frappe.get_doc(
		{
			"doctype": "Delivery Acceptance",
			"delivery_note": delivery_note,
			"condition": condition,
			"damage_notes": damage_notes,
		}
	)
	doc.insert()
	doc.submit()
	return {"acceptance": doc.name, "condition": doc.condition}


@frappe.whitelist()
def invoices(page: int = 0) -> list[dict]:
	return frappe.get_all(
		"Sales Invoice",
		filters={"customer": _dealer("invoices"), "docstatus": 1},
		fields=[
			"name",
			"posting_date",
			"due_date",
			"grand_total",
			"outstanding_amount",
			"currency",
			"status",
			"is_return",
		],
		order_by="posting_date desc",
		limit_start=int(page) * PAGE,
		limit_page_length=PAGE,
		ignore_permissions=True,
	)


@frappe.whitelist()
def support_requests() -> list[dict]:
	return frappe.get_all(
		"Issue",
		filters={"customer": _dealer("support")},
		fields=["name", "subject", "status", "priority", "opening_date"],
		order_by="creation desc",
		limit_page_length=PAGE,
		ignore_permissions=True,
	)


@frappe.whitelist(methods=["POST"])
@idempotent("portal.dealer.create_support_request")
def create_support_request(subject: str, description: str, vehicle: str | None = None) -> dict:
	dealer = _dealer("raise_support")
	if vehicle and frappe.db.get_value("Vehicle Unit", vehicle, "dealer") != dealer:
		raise frappe.PermissionError(_("Not found."))
	issue = frappe.get_doc(
		{
			"doctype": "Issue",
			"subject": subject[:140],
			"description": description,
			"customer": dealer,
			"raised_by": frappe.session.user,
			"issue_type": None,
		}
	)
	if vehicle:
		issue.description = f"{description}\n\nVehicle: {vehicle}"
	issue.flags.ignore_permissions = True
	issue.insert()
	return {"issue": issue.name, "status": issue.status}


@frappe.whitelist()
def outstanding_balance() -> dict:
	dealer = _dealer("invoices")
	rows = frappe.get_all(
		"Sales Invoice",
		filters={"customer": dealer, "docstatus": 1},
		fields=["currency", "outstanding_amount"],
		ignore_permissions=True,
	)
	totals: dict[str, float] = {}
	for row in rows:
		totals[row.currency] = totals.get(row.currency, 0) + flt(row.outstanding_amount)
	return totals
