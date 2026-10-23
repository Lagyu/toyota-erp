"""Vehicle allocation: released vehicles -> open dealer order lines by rule, held with a
serial-level Stock Reservation Entry (ERP-08, J-01, J-09).

Only vehicles that are built, quality-released, in stock in the selling company and
not yet allocated are candidates. Lines are served by the highest-priority active
Allocation Rule for the company (FIFO by order date, dealer tier, or requested date).
Releasing an allocation cancels its reservation and returns the vehicle to the pool;
inventory value never changes because nothing moves until the Delivery Note.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from automotive_core.governance import audit
from automotive_core.utils import commit
from automotive_supply_chain.dealer_operations.orders import _vehicle_warehouses

TIER_ORDER = {"A": 0, "B": 1, "C": 2, None: 3, "": 3}


def _rule(company: str) -> dict | None:
	rules = frappe.get_all("Allocation Rule", filters={"company": company, "is_active": 1},
		fields=["name", "method"], order_by="priority desc, name asc", limit=1)
	return rules[0] if rules else None


def candidates(company: str) -> list[dict]:
	warehouses = _vehicle_warehouses(company)
	if not warehouses:
		return []
	return frappe.get_all("Vehicle Unit", filters={"quality_status": "Released", "inventory_status": "In Stock",
		"production_status": "Built", "automotive_allocation": ("is", "not set"),
		"current_warehouse": ("in", warehouses)}, fields=["name", "variant_item", "current_warehouse", "built_at"],
		order_by="built_at asc, name asc")


def open_lines(company: str) -> list[dict]:
	return frappe.db.sql(
		"""select soi.name, soi.parent, soi.item_code, soi.stock_qty, soi.delivered_qty, soi.conversion_factor,
			ifnull(soi.automotive_allocated_qty, 0) allocated, soi.delivery_date, so.transaction_date, so.creation,
			so.customer, c.automotive_dealer_tier tier
		from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
		join `tabCustomer` c on c.name = so.customer
		where so.docstatus = 1 and so.company = %s and so.status not in ('Closed', 'Completed', 'On Hold')
		and soi.stock_qty - soi.delivered_qty * soi.conversion_factor - ifnull(soi.automotive_allocated_qty, 0) > 0
		and c.is_internal_customer = 0""",
		company,
		as_dict=True,
	)


def _sort_key(method: str):
	if method == "Dealer Priority":
		return lambda r: (TIER_ORDER.get(r.tier, 3), r.transaction_date, r.creation, r.name)
	if method == "Requested Date":
		return lambda r: (r.delivery_date, r.transaction_date, r.name)
	return lambda r: (r.transaction_date, r.creation, r.name)


def _reserve(line, vehicle) -> str:
	from erpnext.stock.doctype.stock_reservation_entry.stock_reservation_entry import get_available_qty_to_reserve

	company = frappe.db.get_value("Sales Order", line.parent, "company")
	sre = frappe.get_doc({
		"doctype": "Stock Reservation Entry",
		"item_code": line.item_code,
		"warehouse": vehicle.current_warehouse,
		"voucher_type": "Sales Order",
		"voucher_no": line.parent,
		"voucher_detail_no": line.name,
		"available_qty": get_available_qty_to_reserve(line.item_code, vehicle.current_warehouse),
		"voucher_qty": line.stock_qty,
		"reserved_qty": 1,
		"stock_uom": frappe.get_cached_value("Item", line.item_code, "stock_uom"),
		"company": company,
		"has_serial_no": 1,
		"reservation_based_on": "Serial and Batch",
		"sb_entries": [{"serial_no": vehicle.name, "qty": 1, "warehouse": vehicle.current_warehouse}],
	})
	sre.flags.ignore_permissions = True
	sre.insert()
	sre.submit()
	return sre.name


def allocate(company: str | None = None) -> list[str]:
	companies = [company] if company else frappe.get_all("Company", pluck="name")
	created = []
	for comp in companies:
		rule = _rule(comp)
		method = rule.method if rule else "Order Date (FIFO)"
		lines = sorted(open_lines(comp), key=_sort_key(method))
		by_variant: dict[str, list] = {}
		for vehicle in candidates(comp):
			by_variant.setdefault(vehicle.variant_item, []).append(vehicle)
		for line in lines:
			pool = by_variant.get(line.item_code) or []
			need = int(flt(line.stock_qty) - flt(line.delivered_qty) * flt(line.conversion_factor) - flt(line.allocated))
			while need > 0 and pool:
				vehicle = pool.pop(0)
				created.append(_allocate_one(line, vehicle, rule.name if rule else None))
				need -= 1
	commit()
	return created


def _allocate_one(line, vehicle, rule: str | None) -> str:
	sre = _reserve(line, vehicle)
	allocation = frappe.get_doc({
		"doctype": "Vehicle Allocation", "vehicle_unit": vehicle.name, "sales_order": line.parent,
		"sales_order_item": line.name, "dealer": line.customer, "variant_item": line.item_code,
		"warehouse": vehicle.current_warehouse, "stock_reservation_entry": sre, "allocation_rule": rule,
		"allocated_at": now_datetime(), "status": "Allocated",
	})
	allocation.flags.ignore_permissions = True
	allocation.insert()
	frappe.db.set_value("Vehicle Unit", vehicle.name, {"inventory_status": "Reserved", "dealer": line.customer,
		"sales_order": line.parent, "automotive_allocation": allocation.name})
	frappe.db.sql("""update `tabSales Order Item` set automotive_allocated_qty = ifnull(automotive_allocated_qty, 0) + 1
		where name = %s""", line.name)
	audit.record("allocation.created", "Vehicle Allocation", allocation.name, {"vin": vehicle.name,
		"sales_order": line.parent, "dealer": line.customer, "rule": rule})
	return allocation.name


def release(allocation_name: str, reason: str) -> None:
	allocation = frappe.get_doc("Vehicle Allocation", allocation_name)
	if allocation.status != "Allocated":
		return
	if allocation.stock_reservation_entry:
		sre = frappe.get_doc("Stock Reservation Entry", allocation.stock_reservation_entry)
		if sre.docstatus == 1:
			sre.flags.ignore_permissions = True
			sre.cancel()
	allocation.db_set({"status": "Released", "release_reason": reason})
	frappe.db.set_value("Vehicle Unit", allocation.vehicle_unit, {"inventory_status": "In Stock", "dealer": None,
		"sales_order": None, "automotive_allocation": None})
	frappe.db.sql("""update `tabSales Order Item` set automotive_allocated_qty = greatest(ifnull(automotive_allocated_qty, 0) - 1, 0)
		where name = %s""", allocation.sales_order_item)
	audit.record("allocation.released", "Vehicle Allocation", allocation.name, {"vin": allocation.vehicle_unit,
		"reason": reason})


def release_for_line(sales_order: str, so_item: str, count: int, reason: str) -> int:
	names = frappe.get_all("Vehicle Allocation", filters={"sales_order": sales_order, "sales_order_item": so_item,
		"status": "Allocated"}, pluck="name", order_by="allocated_at desc", limit=max(count, 0))
	for name in names:
		release(name, reason)
	return len(names)


def release_for_sales_order(sales_order: str, reason: str) -> int:
	names = frappe.get_all("Vehicle Allocation", filters={"sales_order": sales_order, "status": "Allocated"},
		pluck="name")
	for name in names:
		release(name, reason)
	return len(names)


def on_sales_order_submit(doc, method=None) -> None:
	if frappe.get_cached_doc("Supply Chain Settings").auto_allocate:
		allocate(doc.company)


def on_sales_order_cancel(doc, method=None) -> None:
	release_for_sales_order(doc.name, reason=doc.flags.get("cancel_reason") or _("Sales order cancelled"))
	# ERPNext's own on_cancel resets ignore_linked_doctypes; doc events run after it and
	# before the back-link check, so extend it here. These records reference the order by
	# design and keep the history of the cancelled order.
	doc.ignore_linked_doctypes = (*tuple(doc.get("ignore_linked_doctypes") or ()), "Dealer Order",
		"Dealer Order Change", "Vehicle Allocation")


def allocate_all() -> None:
	if frappe.get_cached_doc("Supply Chain Settings").auto_allocate:
		allocate()


@frappe.whitelist(methods=["POST"])
def run_allocation(company: str) -> list[str]:
	frappe.only_for(("Dealer Liaison", "Sales Manager", "System Manager"))
	return allocate(company)


@frappe.whitelist(methods=["POST"])
def release_allocation(name: str, reason: str) -> str:
	frappe.only_for(("Dealer Liaison", "Sales Manager", "System Manager"))
	if not (reason or "").strip():
		frappe.throw(_("A reason is required."))
	release(name, reason)
	return "Released"
