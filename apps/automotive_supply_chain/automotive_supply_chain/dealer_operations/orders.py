"""Dealer registration, dealer orders, promise dates and order changes (ERP-08, J-01, J-09)."""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate, nowdate

from automotive_core.governance import audit
from automotive_core.integration_hub import outbox
from automotive_supply_chain.partners import assert_party_access, is_portal_user

INTERCOMPANY_TRANSIT_DAYS = 21
FROZEN_DAYS = 3


# ---------------------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------------------


def validate_registration(doc) -> None:
	doc.dealer_code = (doc.dealer_code or "").strip().upper()
	if frappe.db.exists("Customer", {"automotive_partner_code": doc.dealer_code}):
		frappe.throw(_("Dealer code {0} is already used.").format(doc.dealer_code))
	if frappe.db.exists("Customer", {"tax_id": doc.tax_id, "disabled": 0}):
		frappe.throw(_("A customer with tax id {0} already exists.").format(doc.tax_id))
	if flt(doc.requested_credit_limit) <= 0:
		frappe.throw(_("Requested credit limit must be positive."))
	if doc.docstatus == 0:
		doc.status = "Draft"


def on_registration_submit(doc) -> None:
	customer = frappe.get_doc({
		"doctype": "Customer",
		"customer_name": doc.dealer_name,
		"customer_type": "Company",
		"customer_group": "Dealers" if frappe.db.exists("Customer Group", "Dealers") else
			frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
		"territory": doc.territory,
		"tax_id": doc.tax_id,
		"default_price_list": doc.price_list,
		"default_currency": frappe.db.get_value("Price List", doc.price_list, "currency"),
		"automotive_partner_code": doc.dealer_code,
		"automotive_dealer_registration": doc.name,
		"automotive_dealer_tier": "B",
		"credit_limits": [{"company": doc.company, "credit_limit": doc.requested_credit_limit}],
	})
	customer.flags.ignore_permissions = True
	customer.insert()
	for contact in doc.contacts:
		if not frappe.db.exists("User", contact.email):
			frappe.get_doc({"doctype": "User", "email": contact.email, "first_name": contact.first_name,
				"last_name": contact.last_name, "user_type": "Website User",
				"send_welcome_email": 0 if frappe.flags.in_test or frappe.flags.in_import else 1}).insert(
				ignore_permissions=True)
		frappe.get_doc({"doctype": "Partner User Access", "user": contact.email, "party_type": "Customer",
			"customer": customer.name, "partner_role": contact.partner_role, "from_date": nowdate()}).insert(
			ignore_permissions=True)
	doc.db_set({"status": "Approved", "customer": customer.name})
	audit.record("dealer.registered", "Customer", customer.name, {"registration": doc.name,
		"credit_limit": flt(doc.requested_credit_limit)}, company=doc.company)


# ---------------------------------------------------------------------------------------
# Promise dates (available-to-promise from stock, capable-to-promise from line capacity)
# ---------------------------------------------------------------------------------------


def _vehicle_warehouses(company: str) -> list[str]:
	return frappe.get_all("Warehouse", filters={"company": company, "is_group": 0,
		"automotive_warehouse_role": ("in", ("Finished Goods", "Outbound"))}, pluck="name")


def free_stock(variant: str, company: str) -> int:
	return frappe.db.count("Vehicle Unit", {"variant_item": variant, "quality_status": "Released",
		"inventory_status": "In Stock", "automotive_allocation": ("is", "not set"),
		"current_warehouse": ("in", _vehicle_warehouses(company) or [""])})


def promise_date(variant: str, qty: int, company: str, requested=None) -> str:
	from automotive_manufacturing.production_planning.snapshot import working_days

	settings = frappe.get_cached_doc("Supply Chain Settings")
	transit = int(settings.promise_transit_days or 7)
	today = getdate(nowdate())
	requested = getdate(requested) if requested else today
	if free_stock(variant, company) >= qty:
		return str(max(requested, add_days(today, transit)))
	template = frappe.get_cached_value("Item", variant, "variant_of") or variant
	model = frappe.get_cached_value("Item", variant, "automotive_vehicle_model") or frappe.get_cached_value(
		"Item", template, "automotive_vehicle_model")
	if not model:
		return str(max(requested, add_days(today, transit + 30)))
	line, plant = frappe.get_cached_value("Vehicle Model", model, ["production_line", "default_plant"])
	capacity = int(frappe.get_cached_value("Production Line", line, "max_units_per_day") or 0)
	build_company = frappe.get_cached_value("Plant", plant, "company")
	extra = INTERCOMPANY_TRANSIT_DAYS if build_company != company else 0
	start = add_days(today, FROZEN_DAYS)
	remaining = qty
	for day in working_days(plant, start, add_days(start, 120)):
		planned = frappe.db.get_value("Plant Schedule", {"production_line": line, "schedule_date": day},
			"planned_units")
		if planned is None:
			planned = frappe.db.count("Work Order", {"automotive_production_line": line, "docstatus": 1,
				"planned_start_date": ("between", (f"{day} 00:00:00", f"{day} 23:59:59"))})
		remaining -= max(capacity - int(planned or 0), 0)
		if remaining <= 0:
			return str(max(requested, add_days(getdate(day), transit + extra)))
	return str(max(requested, add_days(today, 150)))


@frappe.whitelist()
def get_promise(variant_item: str, qty: int, company: str, requested_date: str | None = None) -> str:
	frappe.has_permission("Vehicle Model", "read", throw=True)
	return promise_date(variant_item, cint(qty), company, requested_date)


# ---------------------------------------------------------------------------------------
# Dealer orders
# ---------------------------------------------------------------------------------------


def _price(item_code: str, price_list: str) -> float:
	for code in (item_code, frappe.get_cached_value("Item", item_code, "variant_of")):
		if not code:
			continue
		rate = frappe.db.get_value("Item Price", {"item_code": code, "price_list": price_list, "selling": 1},
			"price_list_rate")
		if rate:
			return flt(rate)
	frappe.throw(_("No dealer price for {0} in {1}.").format(item_code, price_list))


def validate_dealer_order(doc) -> None:
	assert_party_access("Customer", doc.dealer)
	dup = frappe.db.get_value("Dealer Order", {"dealer": doc.dealer, "dealer_reference": doc.dealer_reference,
		"docstatus": ("<", 2), "name": ("!=", doc.name)}, "name")
	if dup:
		frappe.throw(_("Dealer reference {0} was already used ({1}).").format(doc.dealer_reference, dup))
	registration = frappe.db.get_value("Customer", doc.dealer, "automotive_dealer_registration")
	authorised = set(frappe.get_all("Dealer Authorised Model", filters={"parent": registration}, pluck="vehicle_model")) \
		if registration else set()
	price_list = frappe.db.get_value("Customer", doc.dealer, "default_price_list")
	if not price_list:
		frappe.throw(_("Dealer {0} has no price list.").format(doc.dealer))
	doc.currency = frappe.db.get_value("Price List", price_list, "currency")
	portal = is_portal_user()
	total = 0.0
	for item in doc.items:
		if authorised and item.vehicle_model not in authorised:
			frappe.throw(_("Row {0}: {1} is not authorised for this dealer.").format(item.idx, item.vehicle_model))
		config = frappe.db.get_value("Vehicle Configuration", item.configuration, ["status", "vehicle_model",
			"variant_item"], as_dict=True)
		if not config or config.status != "Valid" or config.vehicle_model != item.vehicle_model:
			frappe.throw(_("Row {0}: configuration {1} is not a valid {2} configuration.").format(
				item.idx, item.configuration, item.vehicle_model))
		item.variant_item = config.variant_item
		item.list_price = _price(config.variant_item, price_list)
		if portal:
			item.discount_percent = 0  # discounts are granted internally, never self-assigned
		item.amount = flt(item.list_price) * cint(item.qty) * (1 - flt(item.discount_percent) / 100)
		total += item.amount
	doc.total = total
	if doc.docstatus == 0:
		doc.status = "Draft"


def on_dealer_order_submit(doc) -> None:
	doc.db_set("status", "Submitted")
	outbox.publish("dealer_order.received", {"dealer_order": doc.name, "reference": doc.dealer_reference,
		"status": "Submitted"}, doc.doctype, doc.name, party_type="Customer", party=doc.dealer)


def _vehicle_warehouse(company: str) -> str:
	warehouses = frappe.get_all("Warehouse", filters={"company": company, "is_group": 0,
		"automotive_warehouse_role": "Finished Goods"}, pluck="name", order_by="name asc")
	if not warehouses:
		frappe.throw(_("{0} has no finished-goods warehouse.").format(company))
	return warehouses[0]


@frappe.whitelist(methods=["POST"])
def accept(name: str) -> dict:
	frappe.only_for(("Dealer Liaison", "Sales Manager", "System Manager"))
	from automotive_core.governance.approvals import find_policy, request_approval

	doc = frappe.get_doc("Dealer Order", name)
	if doc.docstatus != 1 or doc.status != "Submitted":
		frappe.throw(_("Dealer Order {0} is {1}.").format(name, doc.status))
	warehouse = _vehicle_warehouse(doc.company)
	promised = max(getdate(promise_date(i.variant_item, cint(i.qty), doc.company, doc.requested_delivery_date))
		for i in doc.items)
	so = frappe.new_doc("Sales Order")
	so.update({"customer": doc.dealer, "company": doc.company, "transaction_date": nowdate(),
		"delivery_date": promised, "set_warehouse": warehouse, "po_no": doc.dealer_reference,
		"automotive_dealer_order": doc.name, "automotive_promised_date": promised,
		"selling_price_list": frappe.db.get_value("Customer", doc.dealer, "default_price_list")})
	for item in doc.items:
		so.append("items", {"item_code": item.variant_item, "qty": item.qty, "price_list_rate": item.list_price,
			"discount_percentage": item.discount_percent, "delivery_date": promised, "warehouse": warehouse,
			"automotive_configuration": item.configuration})
	so.flags.ignore_permissions = True
	so.insert()
	needs_approval = bool(find_policy(so))
	if needs_approval:
		request_approval(so.doctype, so.name)
	else:
		so.submit()
	doc.db_set({"status": "Accepted", "promised_date": promised, "sales_order": so.name})
	outbox.publish("dealer_order.accepted", {"dealer_order": doc.name, "reference": doc.dealer_reference,
		"sales_order": so.name, "promised_date": str(promised)}, doc.doctype, doc.name, party_type="Customer",
		party=doc.dealer)
	return {"sales_order": so.name, "promised_date": str(promised), "needs_approval": needs_approval}


@frappe.whitelist(methods=["POST"])
def reject(name: str, reason: str) -> str:
	frappe.only_for(("Dealer Liaison", "Sales Manager", "System Manager"))
	doc = frappe.get_doc("Dealer Order", name)
	if doc.status != "Submitted":
		frappe.throw(_("Dealer Order {0} is {1}.").format(name, doc.status))
	doc.db_set({"status": "Rejected", "rejection_reason": reason})
	outbox.publish("dealer_order.rejected", {"dealer_order": doc.name, "reason": reason}, doc.doctype, doc.name,
		party_type="Customer", party=doc.dealer)
	return "Rejected"


def validate_sales_order(doc, method=None) -> None:
	doc.automotive_max_discount_percent = max((flt(i.discount_percentage) for i in doc.items), default=0)
	for item in doc.items:
		if item.get("automotive_configuration"):
			variant = frappe.db.get_value("Vehicle Configuration", item.automotive_configuration, "variant_item")
			if variant and variant != item.item_code:
				frappe.throw(_("Row {0}: configuration {1} resolves to {2}, not {3}.").format(
					item.idx, item.automotive_configuration, variant, item.item_code))


# ---------------------------------------------------------------------------------------
# Changes after acceptance (J-09)
# ---------------------------------------------------------------------------------------


def validate_change(doc) -> None:
	order = frappe.get_doc("Dealer Order", doc.dealer_order)
	assert_party_access("Customer", order.dealer)
	if order.status != "Accepted" or not order.sales_order:
		frappe.throw(_("Only accepted orders can be changed (status {0}).").format(order.status))
	doc.sales_order, doc.dealer = order.sales_order, order.dealer
	so = frappe.get_doc("Sales Order", order.sales_order)
	if doc.change_type != "Cancel Order":
		if not 1 <= cint(doc.item_row) <= len(so.items):
			frappe.throw(_("Order line {0} does not exist.").format(doc.item_row))
		row = so.items[cint(doc.item_row) - 1]
		if doc.change_type == "Change Quantity" and cint(doc.new_qty) < flt(row.delivered_qty):
			frappe.throw(_("Cannot go below the {0} already delivered.").format(row.delivered_qty))
		if doc.change_type == "Change Configuration":
			if flt(row.delivered_qty):
				frappe.throw(_("A delivered line cannot change configuration."))
			cfg = frappe.db.get_value("Vehicle Configuration", doc.new_configuration, ["status", "variant_item"],
				as_dict=True)
			if not cfg or cfg.status != "Valid":
				frappe.throw(_("New configuration must be valid."))
	elif any(flt(i.delivered_qty) for i in so.items):
		frappe.throw(_("Delivered orders cannot be cancelled; use a return instead."))
	if doc.docstatus == 0:
		doc.status = "Draft"


def _trans_items(so, overrides: dict[str, dict] | None = None, drop: set[str] | None = None, add: list | None = None):
	rows = []
	for item in so.items:
		if drop and item.name in drop:
			continue
		row = {"docname": item.name, "item_code": item.item_code, "qty": item.qty, "rate": item.rate,
			"delivery_date": str(item.delivery_date), "conversion_factor": item.conversion_factor, "uom": item.uom}
		row.update((overrides or {}).get(item.name, {}))
		rows.append(row)
	rows.extend(add or [])
	return json.dumps(rows, default=str)


def apply_change(doc) -> str:
	from erpnext.controllers.accounts_controller import update_child_qty_rate

	from automotive_supply_chain.dealer_operations import allocation

	so = frappe.get_doc("Sales Order", doc.sales_order)
	order = frappe.get_doc("Dealer Order", doc.dealer_order)
	if doc.change_type == "Cancel Order":
		released = allocation.release_for_sales_order(so.name, reason=doc.reason)
		so.flags.cancel_reason = _("Dealer order change {0}: {1}").format(doc.name, doc.reason)
		so.flags.ignore_permissions = True
		so.cancel()
		order.db_set("status", "Cancelled")
		effect = _("Sales order {0} cancelled; {1} vehicle allocation(s) released.").format(so.name, released)
	else:
		row = so.items[cint(doc.item_row) - 1]
		if doc.change_type == "Change Quantity":
			allocated = flt(row.automotive_allocated_qty)
			keep = max(cint(doc.new_qty) - flt(row.delivered_qty), 0)
			released = allocation.release_for_line(so.name, row.name, int(max(allocated - keep, 0)), doc.reason)
			update_child_qty_rate("Sales Order", _trans_items(so, {row.name: {"qty": cint(doc.new_qty)}}), so.name)
			effect = _("Line {0}: qty {1} -> {2}; {3} allocation(s) released.").format(doc.item_row, row.qty,
				doc.new_qty, released)
		elif doc.change_type == "Change Delivery Date":
			update_child_qty_rate("Sales Order", _trans_items(so, {row.name: {"delivery_date": str(doc.new_delivery_date)}}),
				so.name)
			effect = _("Line {0}: delivery date {1} -> {2}.").format(doc.item_row, row.delivery_date, doc.new_delivery_date)
		else:
			released = allocation.release_for_line(so.name, row.name, int(flt(row.automotive_allocated_qty)), doc.reason)
			variant = frappe.db.get_value("Vehicle Configuration", doc.new_configuration, "variant_item")
			price_list = so.selling_price_list
			rate = flt(frappe.db.get_value("Item Price", {"item_code": variant, "price_list": price_list}, "price_list_rate")
				or frappe.db.get_value("Item Price", {"item_code": frappe.get_cached_value("Item", variant, "variant_of"),
					"price_list": price_list}, "price_list_rate")) * (1 - flt(row.discount_percentage) / 100)
			update_child_qty_rate("Sales Order", _trans_items(so, drop={row.name}, add=[{"item_code": variant,
				"qty": row.qty, "rate": rate, "delivery_date": str(row.delivery_date), "conversion_factor": 1,
				"uom": row.uom}]), so.name)
			so.reload()
			new_row = next(i for i in so.items if i.item_code == variant and not i.automotive_configuration)
			new_row.db_set("automotive_configuration", doc.new_configuration)
			effect = _("Line {0}: {1} -> {2}; {3} allocation(s) released.").format(doc.item_row, row.item_code,
				variant, released)
		allocation.allocate(so.company)
	audit.record("dealer_order.changed", "Sales Order", so.name, {"change": doc.name, "type": doc.change_type,
		"effect": effect}, company=so.company)
	outbox.publish("dealer_order.changed", {"dealer_order": order.name, "change": doc.change_type, "effect": effect},
		doc.doctype, doc.name, party_type="Customer", party=order.dealer)
	return effect
