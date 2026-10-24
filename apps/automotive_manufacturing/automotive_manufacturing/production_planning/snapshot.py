"""Collect the complete, self-contained input snapshot for a planning run (ERP-03).

Everything the engine reads is captured here (demand, stock, scheduled receipts, BOM
revisions with effectivity, lead times, calendars, capacities, parameter version),
so ``engine.mrp.plan(snapshot)`` can be re-run later from the stored file alone.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

import frappe
from frappe.utils import add_days, flt, getdate

from automotive_core.utils import hook_list

EXCLUDED_ROLES = ("Quarantine", "Scrap", "In Transit", "Consignment", "Receiving Inspection")


def plant_warehouses(plant: str) -> tuple[list[str], list[str]]:
	"""(nettable warehouses, all leaf warehouses) of a plant."""
	rows = frappe.get_all(
		"Warehouse",
		filters={"automotive_plant": plant, "is_group": 0, "disabled": 0},
		fields=["name", "automotive_warehouse_role"],
	)
	nettable = [r.name for r in rows if r.automotive_warehouse_role not in EXCLUDED_ROLES]
	return sorted(nettable), sorted(r.name for r in rows)


def working_days(plant: str, start: dt.date, end: dt.date) -> list[str]:
	pattern_name = frappe.get_cached_value("Plant", plant, "default_shift_pattern")
	pattern = frappe.get_cached_doc("Shift Pattern", pattern_name) if pattern_name else None
	holidays = set()
	for hl in frappe.get_all(
		"Holiday List", filters={"to_date": (">=", start), "from_date": ("<=", end)}, pluck="name"
	):
		if pattern and pattern.holiday_list and hl.split(" ")[0] != pattern.holiday_list.split(" ")[0]:
			continue
		holidays |= {
			getdate(d) for d in frappe.get_all("Holiday", filters={"parent": hl}, pluck="holiday_date")
		}
	days, day = [], start
	weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
	while day <= end:
		if (not pattern or pattern.get(weekdays[day.weekday()])) and day not in holidays:
			days.append(day.isoformat())
		day += dt.timedelta(days=1)
	return days


def _boms_for(item_code: str) -> list[dict]:
	return frappe.get_all(
		"BOM",
		filters={"item": item_code, "docstatus": 1, "is_active": 1},
		fields=[
			"name",
			"item",
			"quantity",
			"is_default",
			"automotive_effective_from",
			"automotive_effective_to",
			"process_loss_percentage",
		],
		order_by="name asc",
	)


def _preferred_supplier(item_code: str, company: str) -> str | None:
	for path in hook_list("automotive_planning_supplier_resolvers"):
		supplier = frappe.get_attr(path)(item_code, company)
		if supplier:
			return supplier
	return frappe.db.get_value("Item Default", {"parent": item_code, "company": company}, "default_supplier")


def collect(run, params) -> dict[str, Any]:
	as_of = getdate(run.as_of)
	horizon_end = add_days(as_of, int(params.horizon_days))
	company, plant = run.company, run.plant
	nettable, _all = plant_warehouses(plant)
	plant_doc = frappe.get_cached_doc("Plant", plant)

	demand: list[dict] = []
	# firm orders (sales orders pending delivery from this plant's warehouses)
	so_rows = frappe.db.sql(
		"""select soi.item_code, (soi.stock_qty - soi.delivered_qty * soi.conversion_factor) qty,
			soi.delivery_date, so.name
		from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
		where so.docstatus = 1 and so.company = %s and so.status not in ('Closed', 'Completed', 'On Hold')
		and soi.warehouse in %s and soi.delivery_date <= %s
		and soi.stock_qty > soi.delivered_qty * soi.conversion_factor""",
		(company, tuple(nettable) or ("",), horizon_end),
		as_dict=True,
	)
	firm_by_week: dict[tuple[str, int], float] = defaultdict(float)
	for row in so_rows:
		demand.append(
			{
				"item_code": row.item_code,
				"qty": flt(row.qty),
				"date": str(row.delivery_date),
				"source": f"SO:{row.name}",
			}
		)
		firm_by_week[(row.item_code, getdate(row.delivery_date).isocalendar()[1])] += flt(row.qty)
	# forecast via the native Master Production Schedule
	if params.include_forecast and run.master_production_schedule:
		for row in frappe.get_all(
			"Master Production Schedule Item",
			filters={"parent": run.master_production_schedule},
			fields=["item_code", "planned_qty", "delivery_date"],
			order_by="idx asc",
		):
			day = getdate(row.delivery_date)
			if not (as_of <= day <= horizon_end):
				continue
			qty = flt(row.planned_qty)
			if params.consume_forecast:
				key = (row.item_code, day.isocalendar()[1])
				consumed = min(qty, firm_by_week[key])
				firm_by_week[key] -= consumed
				qty -= consumed
			if qty > 0:
				demand.append(
					{
						"item_code": row.item_code,
						"qty": qty,
						"date": day.isoformat(),
						"source": f"MPS:{run.master_production_schedule}",
					}
				)
	# components still owed to released work orders
	for row in frappe.db.sql(
		"""select woi.item_code, (woi.required_qty - woi.consumed_qty) qty, wo.planned_start_date, wo.name
		from `tabWork Order Item` woi join `tabWork Order` wo on wo.name = woi.parent
		where wo.docstatus = 1 and wo.company = %s and wo.automotive_plant = %s
		and wo.status not in ('Completed', 'Closed', 'Stopped') and woi.required_qty > woi.consumed_qty""",
		(company, plant),
		as_dict=True,
	):
		demand.append(
			{
				"item_code": row.item_code,
				"qty": flt(row.qty),
				"date": str(getdate(row.planned_start_date)),
				"source": f"WO-REQ:{row.name}",
			}
		)

	# explode the item universe through effective BOM revisions
	items: dict[str, dict] = {}
	boms: dict[str, dict] = {}
	queue = sorted({d["item_code"] for d in demand})
	seen: set[str] = set()
	while queue:
		code = queue.pop(0)
		if code in seen:
			continue
		seen.add(code)
		item = frappe.get_cached_value(
			"Item",
			code,
			[
				"stock_uom",
				"lead_time_days",
				"safety_stock",
				"min_order_qty",
				"is_purchase_item",
				"automotive_kanban_controlled",
			],
			as_dict=True,
		)
		if not item:
			continue
		item_boms = _boms_for(code)
		for bom in item_boms:
			lines = frappe.get_all(
				"BOM Item",
				filters={"parent": bom.name},
				fields=["item_code", "stock_qty"],
				order_by="idx asc",
			)
			boms[bom.name] = {
				"item": code,
				"quantity": flt(bom.quantity),
				"is_default": bom.is_default,
				"effective_from": str(bom.automotive_effective_from)
				if bom.automotive_effective_from
				else None,
				"effective_to": str(bom.automotive_effective_to) if bom.automotive_effective_to else None,
				"lines": [{"item_code": li.item_code, "qty": flt(li.stock_qty)} for li in lines],
			}
			if flt(
				bom.process_loss_percentage
			):  # the yield (only when there is a loss: older snapshots replay alike)
				boms[bom.name]["process_loss_percent"] = flt(bom.process_loss_percentage)
			queue.extend(sorted(li.item_code for li in lines if li.item_code not in seen))
		manufactured = bool(item_boms)
		items[code] = {
			"uom": item.stock_uom,
			"lead_time_days": int(item.lead_time_days or (1 if manufactured else 7)),
			"safety_stock": flt(item.safety_stock),
			"min_order_qty": flt(item.min_order_qty),
			"is_purchased": not manufactured,
			"supplier": None if manufactured else _preferred_supplier(code, company),
			"warehouse": plant_doc.finished_goods_warehouse
			if manufactured
			else plant_doc.raw_material_warehouse,
			"line": _line_for(code, plant),
			"kanban": bool(item.automotive_kanban_controlled),
		}

	on_hand: dict[str, float] = {}
	if items:
		for row in frappe.db.sql(
			"""select item_code, sum(actual_qty - reserved_stock) qty from `tabBin`
			where warehouse in %s and item_code in %s group by item_code""",
			(tuple(nettable) or ("",), tuple(items)),
			as_dict=True,
		):
			on_hand[row.item_code] = flt(row.qty)

	receipts: list[dict] = []
	if items:
		for row in frappe.db.sql(
			"""select poi.item_code, (poi.stock_qty - poi.received_qty * poi.conversion_factor) qty,
				poi.schedule_date, po.name
			from `tabPurchase Order Item` poi join `tabPurchase Order` po on po.name = poi.parent
			where po.docstatus = 1 and po.company = %s and po.status not in ('Closed', 'Completed', 'On Hold')
			and poi.warehouse in %s and poi.item_code in %s and poi.stock_qty > poi.received_qty * poi.conversion_factor""",
			(company, tuple(nettable) or ("",), tuple(items)),
			as_dict=True,
		):
			receipts.append(
				{
					"item_code": row.item_code,
					"qty": flt(row.qty),
					"date": str(row.schedule_date),
					"source": f"PO:{row.name}",
				}
			)
		for row in frappe.db.sql(
			"""select production_item, (qty - produced_qty) qty, ifnull(planned_end_date, planned_start_date) day, name
			from `tabWork Order` where docstatus = 1 and company = %s and automotive_plant = %s
			and status not in ('Completed', 'Closed', 'Stopped') and qty > produced_qty and production_item in %s""",
			(company, plant, tuple(items)),
			as_dict=True,
		):
			receipts.append(
				{
					"item_code": row.production_item,
					"qty": flt(row.qty),
					"date": str(getdate(row.day)),
					"source": f"WO:{row.name}",
				}
			)

	lines = {}
	for line in sorted({i["line"] for i in items.values() if i["line"]}):
		lines[line] = {
			"daily_capacity": int(frappe.get_cached_value("Production Line", line, "max_units_per_day") or 0)
		}

	return {
		"schema": "automotive.mrp.snapshot/v1",
		"as_of": as_of.isoformat(),
		"company": company,
		"plant": plant,
		"parameter_set": params.name,
		"parameters": {
			"horizon_days": int(params.horizon_days),
			"frozen_zone_days": int(params.frozen_zone_days or 0),
			"lot_sizing": params.lot_sizing,
			"safety_stock_policy": params.safety_stock_policy,
			"days_of_cover": flt(params.days_of_cover),
			"lead_time_source": params.lead_time_source,
			"fixed_lead_time_days": int(params.fixed_lead_time_days or 0),
			"include_kanban_items": bool(params.include_kanban_items),
			"capacity_check": bool(params.capacity_check),
		},
		"working_days": working_days(plant, as_of, horizon_end) if params.respect_holidays else [],
		"items": items,
		"boms": boms,
		"on_hand": on_hand,
		"scheduled_receipts": sorted(receipts, key=lambda r: (r["item_code"], r["date"], r["source"])),
		"demand": sorted(demand, key=lambda r: (r["item_code"], r["date"], r["source"])),
		"lines": lines,
	}


def _line_for(item_code: str, plant: str) -> str | None:
	model = frappe.get_cached_value("Item", item_code, "automotive_vehicle_model")
	variant_of = frappe.get_cached_value("Item", item_code, "variant_of")
	if not model and variant_of:
		model = frappe.get_cached_value("Item", variant_of, "automotive_vehicle_model")
	if model:
		line = frappe.get_cached_value("Vehicle Model", model, "production_line")
		if line and frappe.get_cached_value("Production Line", line, "plant") == plant:
			return line
	return None
