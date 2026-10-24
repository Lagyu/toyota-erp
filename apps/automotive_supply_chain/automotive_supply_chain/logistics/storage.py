"""Storage: put-away, forward-store reorder rules and picking at the plants (ERP-06).

A plant's raw material store is its forward store, next to the lines; its reserve storage
(the high-bay) holds the rest. Each stored part has:

- put-away rules: receipts fill the forward store up to the part's capacity and store the
  rest in reserve storage (ERPNext's Putaway Rule, applied when a receipt is saved);
- a reorder rule on the forward store (ERPNext's Item Reorder, type Transfer): when the
  forward store's on-hand quantity falls to the reorder level, the daily replenishment run
  raises a material transfer request from reserve storage;
- the warehouse clerk fulfils each request with a pick list from reserve storage and the
  transfer entry made from it.

ERPNext's own reorder job (Stock Settings: auto indent) compares the projected quantity,
which counts open purchase orders into the forward store, so it would not move stock from
reserve while an order is open; the replenishment run compares on-hand stock instead, and
the tenant leaves auto indent off. Every step is a standard ERPNext document (Putaway Rule,
Item Reorder, Material Request, Pick List, Stock Entry), each linked to the next.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, nowdate

from automotive_core.governance import audit

# Reserve storage takes whatever the forward store cannot: ERPNext drops a received quantity
# that fits no rule from the receipt (with only a message), so the reserve never fills.
RESERVE_CAPACITY = 10**9


def _plant(plant: str):
	doc = frappe.get_cached_doc("Plant", plant)
	if not doc.raw_material_warehouse or not doc.reserve_warehouse:
		frappe.throw(_("Plant {0} needs a raw material store and reserve storage.").format(plant))
	return doc


def set_storage(item_code: str, plant: str, capacity: float, reorder_level: float, reorder_qty: float) -> list[str]:
	"""A part's storage at a plant: forward store up to ``capacity`` then reserve storage,
	and the transfer reorder rule on the forward store. Idempotent; returns what changed."""
	doc = _plant(plant)
	if not 0 <= flt(reorder_level) < flt(capacity) or flt(reorder_qty) <= 0:
		frappe.throw(_("The reorder level must be below the forward capacity, and the reorder quantity positive."))
	changed = []
	for warehouse, priority, limit in ((doc.raw_material_warehouse, 1, capacity),
			(doc.reserve_warehouse, 2, RESERVE_CAPACITY)):
		name = frappe.db.get_value("Putaway Rule", {"item_code": item_code, "warehouse": warehouse}, "name")
		rule = frappe.get_doc("Putaway Rule", name) if name else frappe.new_doc("Putaway Rule")
		values = {"item_code": item_code, "warehouse": warehouse, "company": doc.company, "priority": priority,
			"capacity": flt(limit), "uom": frappe.get_cached_value("Item", item_code, "stock_uom"), "disable": 0}
		if rule.is_new() or any(rule.get(k) != v for k, v in values.items()):
			rule.update(values)
			rule.save()
			changed.append(f"putaway {item_code} -> {warehouse} ({flt(limit):g})")
	item = frappe.get_doc("Item", item_code)
	row = next((r for r in item.reorder_levels if r.warehouse == doc.raw_material_warehouse), None)
	values = {"warehouse": doc.raw_material_warehouse, "warehouse_reorder_level": flt(reorder_level),
		"warehouse_reorder_qty": flt(reorder_qty), "material_request_type": "Transfer"}
	if row is None or any(row.get(k) != v for k, v in values.items()):
		if row is None:
			item.append("reorder_levels", values)
		else:
			row.update(values)
		item.save()
		changed.append(f"reorder {item_code} at {doc.raw_material_warehouse}")
	return changed


def putaway_applies(receipt) -> bool:
	"""Put-away for a receipt into a plant's forward store whose parts have put-away rules.
	ERPNext applies the rules to every line of a document, so a receipt that also delivers
	elsewhere (line side, say) is left as it is."""
	rows = receipt.get("items") or []  # a document or a plain dict
	warehouses = {row.warehouse for row in rows}
	if len(warehouses) != 1:
		return False
	forward = warehouses.pop()
	if not frappe.db.exists("Plant", {"raw_material_warehouse": forward, "reserve_warehouse": ("is", "set")}):
		return False
	return bool(frappe.db.exists("Putaway Rule", {"company": receipt.company, "disable": 0,
		"item_code": ("in", sorted({row.item_code for row in rows}))}))


def _open_requests(forward: str) -> dict[str, float]:
	"""Quantity still to transfer into the forward store, per item, on submitted requests."""
	return dict(frappe.db.sql("""select mri.item_code, sum(mri.stock_qty - ifnull(mri.transferred_qty, 0))
		from `tabMaterial Request Item` mri join `tabMaterial Request` mr on mr.name = mri.parent
		where mr.docstatus = 1 and mr.material_request_type = 'Material Transfer'
		and mr.status not in ('Stopped', 'Cancelled', 'Transferred') and mri.warehouse = %s
		and mri.stock_qty > ifnull(mri.transferred_qty, 0) group by mri.item_code""", forward))


def raise_transfer_requests(plant: str) -> str | None:
	"""The day's replenishment of a plant's forward store: one transfer request from reserve
	storage for every part at or below its reorder level (on hand plus already requested),
	for its reorder quantity within the forward capacity and what the reserve holds."""
	doc = _plant(plant)
	forward, reserve = doc.raw_material_warehouse, doc.reserve_warehouse
	rules = frappe.db.sql("""select ir.parent item_code, ir.warehouse_reorder_level level, ir.warehouse_reorder_qty qty,
		pr.stock_capacity capacity from `tabItem Reorder` ir
		join `tabPutaway Rule` pr on pr.item_code = ir.parent and pr.warehouse = ir.warehouse and pr.disable = 0
		join `tabItem` i on i.name = ir.parent and i.disabled = 0
		where ir.warehouse = %s and ir.material_request_type = 'Transfer' order by ir.parent""", forward, as_dict=True)
	if not rules:
		return None
	stock = {(r.item_code, r.warehouse): flt(r.actual_qty) for r in frappe.get_all("Bin",
		filters={"warehouse": ("in", (forward, reserve)), "item_code": ("in", [r.item_code for r in rules])},
		fields=["item_code", "warehouse", "actual_qty"])}
	requested = _open_requests(forward)
	lines = []
	for rule in rules:
		on_hand = stock.get((rule.item_code, forward), 0) + flt(requested.get(rule.item_code))
		if on_hand > flt(rule.level):
			continue
		qty = min(max(flt(rule.qty), flt(rule.level) - on_hand), flt(rule.capacity) - on_hand,
			stock.get((rule.item_code, reserve), 0))
		item = frappe.get_cached_value("Item", rule.item_code, ["item_name", "description", "item_group", "stock_uom"],
			as_dict=True)
		if frappe.get_cached_value("UOM", item.stock_uom, "must_be_whole_number"):
			qty = int(qty)  # never more than the reserve holds
		if qty <= 0:
			continue
		# the line as ERPNext's own reorder job writes it (erpnext.stock.reorder_item)
		lines.append({"item_code": rule.item_code, "item_name": item.item_name, "description": item.description,
			"item_group": item.item_group, "qty": qty, "uom": item.stock_uom, "stock_uom": item.stock_uom,
			"conversion_factor": 1, "warehouse": forward, "from_warehouse": reserve, "schedule_date": nowdate(),
			"reorder_level": rule.level, "reorder_qty": rule.qty, "projected_on_hand": on_hand})
	if not lines:
		return None
	request = frappe.get_doc({"doctype": "Material Request", "material_request_type": "Material Transfer",
		"company": doc.company, "transaction_date": nowdate(), "schedule_date": nowdate(), "auto_created_via_reorder": 1,
		"set_warehouse": forward, "set_from_warehouse": reserve, "items": lines})
	request.flags.ignore_permissions = True  # the scheduled run, as the system
	request.insert()
	request.submit()
	audit.record("storage.replenishment_requested", request.doctype, request.name, {"plant": plant,
		"lines": len(lines)}, company=doc.company)
	return request.name


def raise_all_transfer_requests() -> None:
	"""Scheduled daily: the replenishment run of every plant with reserve storage."""
	for plant in frappe.get_all("Plant", filters={"reserve_warehouse": ("is", "set")}, pluck="name"):
		raise_transfer_requests(plant)


def open_transfer_requests(plant: str) -> list[str]:
	"""Submitted transfer requests into the plant's forward store with quantity still to move."""
	doc = _plant(plant)
	return frappe.db.sql_list(
		"""select distinct mr.name from `tabMaterial Request` mr
		join `tabMaterial Request Item` mri on mri.parent = mr.name
		where mr.docstatus = 1 and mr.material_request_type = 'Material Transfer' and mr.company = %s
		and mr.status not in ('Stopped', 'Cancelled', 'Transferred') and mri.warehouse = %s
		and mri.stock_qty > ifnull(mri.transferred_qty, 0)
		order by mr.transaction_date, mr.name""", (doc.company, doc.raw_material_warehouse))


def pick_from_reserve(material_request: str, plant: str) -> tuple[str, str] | None:
	"""The clerk fulfils a transfer request from reserve storage: a pick list (locations from
	the reserve only, oldest stock first) and the transfer entry made from it. None when the
	reserve holds none of the requested parts."""
	from erpnext.stock.doctype.material_request.material_request import create_pick_list
	from erpnext.stock.doctype.pick_list.pick_list import create_stock_entry

	doc = _plant(plant)
	items = frappe.get_all("Material Request Item", filters={"parent": material_request}, pluck="item_code")
	if not frappe.db.exists("Bin", {"warehouse": doc.reserve_warehouse, "item_code": ("in", items),
			"actual_qty": (">", 0)}):
		return None
	pick_list = create_pick_list(material_request)
	pick_list.parent_warehouse = doc.reserve_warehouse  # the locations are chosen again on save
	pick_list.insert()
	pick_list.submit()
	entry = frappe.get_doc(create_stock_entry(pick_list.as_dict()))
	entry.insert()
	entry.submit()
	return pick_list.name, entry.name
