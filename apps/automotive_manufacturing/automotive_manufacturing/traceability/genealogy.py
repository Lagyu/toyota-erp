"""Lot-to-vehicle and vehicle-to-component genealogy (ERP-02, ERP-07, J-08).

On submit of a Manufacture Stock Entry, every consumed serial/batch is linked to the
unit(s) or batch the entry produced. Manufacture entries for serialized products are
posted one unit per entry (mes.production), so the link is exact; for batch-built
products the produced batch is the parent. Purchase receipts stamp the supplier and
supplier lot on incoming batches, which lets a recall start from a supplier lot.

Queries walk the links in both directions:
* ``components_of(serial)``  -> full upstream tree (vehicle -> powertrain -> lots)
* ``units_containing(item, batches, serials)`` -> every finished unit downstream
"""

from __future__ import annotations

from collections import deque

import frappe
from frappe.utils import now_datetime


def _bundle(row) -> str | None:
	"""The row's Serial and Batch Bundle. Serials and batches numbered automatically are
	bundled while the entry posts, after the in-memory row was built: read it back."""
	return row.serial_and_batch_bundle or (frappe.db.get_value("Stock Entry Detail", row.name,
		"serial_and_batch_bundle") if row.name else None)


def _consumed(stock_entry) -> list[dict]:
	from erpnext.stock.serial_batch_bundle import get_batch_nos, get_serial_nos

	rows = []
	for row in stock_entry.items:
		if not row.s_warehouse or row.is_finished_item or row.get("is_scrap_item") or row.get("type") == "Scrap":
			continue
		serials = [s.strip() for s in (row.serial_no or "").split("\n") if s.strip()]
		batches = {row.batch_no: row.transfer_qty or row.qty} if row.batch_no else {}
		bundle = _bundle(row) if not serials and not batches else None
		if bundle:
			serials = get_serial_nos(bundle)
			batches = {b: abs(d.get("qty", 0)) for b, d in (get_batch_nos(bundle) or {}).items()}
		for serial in serials:
			rows.append({"item": row.item_code, "serial": serial, "batch": None, "qty": 1})
		for batch, qty in batches.items():
			rows.append({"item": row.item_code, "serial": None, "batch": batch, "qty": qty})
	return rows


def _produced(stock_entry) -> list[dict]:
	from erpnext.stock.serial_batch_bundle import get_batch_nos, get_serial_nos

	out = []
	for row in stock_entry.items:
		if not (row.is_finished_item and row.t_warehouse):
			continue
		serials = [s.strip() for s in (row.serial_no or "").split("\n") if s.strip()]
		batch = row.batch_no
		bundle = _bundle(row) if not serials and not batch else None
		if bundle:
			serials = get_serial_nos(bundle)
			batch = next(iter(get_batch_nos(bundle) or {}), None)
		out.append({"item": row.item_code, "serials": serials, "batch": batch})
	return out


def _supplier_of(item: str, serial: str | None, batch: str | None) -> tuple[str | None, str | None]:
	if batch:
		values = frappe.db.get_value("Batch", batch, ["automotive_supplier", "automotive_supplier_lot"])
		if values:
			return values[0], values[1]
	if serial:
		# v16 Serial No has no supplier column: take it from the inward receipt of the serial
		receipt = frappe.db.sql(
			"""select pr.supplier from `tabSerial and Batch Entry` sbe
			join `tabSerial and Batch Bundle` sbb on sbb.name = sbe.parent
			join `tabPurchase Receipt` pr on pr.name = sbb.voucher_no
			where sbe.serial_no = %s and sbb.voucher_type = 'Purchase Receipt' and sbb.docstatus = 1
			order by sbb.creation asc limit 1""",
			serial,
		)
		return (receipt[0][0] if receipt else None), None
	return None, None


def on_stock_entry_submit(doc, method=None) -> None:
	if doc.purpose not in ("Manufacture", "Repack"):
		return
	consumed = _consumed(doc)
	if not consumed:
		return
	now = now_datetime()
	for parent in _produced(doc):
		targets = [(s, None) for s in parent["serials"]] or [(None, parent["batch"])]
		share = 1 / len(targets)
		for parent_serial, parent_batch in targets:
			if not parent_serial and not parent_batch:
				continue
			for comp in consumed:
				supplier, lot = _supplier_of(comp["item"], comp["serial"], comp["batch"])
				link = frappe.get_doc(
					{
						"doctype": "Genealogy Link",
						"parent_item": parent["item"],
						"parent_serial_no": parent_serial,
						"parent_batch_no": parent_batch,
						"component_item": comp["item"],
						"component_serial_no": comp["serial"],
						"component_batch_no": comp["batch"],
						"qty": comp["qty"] * share,
						"supplier": supplier,
						"supplier_lot": lot,
						"stock_entry": doc.name,
						"work_order": doc.work_order,
						"plant": doc.get("automotive_plant"),
						"recorded_at": now,
					}
				)
				link.flags.ignore_permissions = True
				link.db_insert()


def backfill(limit: int | None = None) -> int:
	"""Record the links of submitted manufacture entries that have none (entries posted
	before auto-numbered serials/batches were read back). Idempotent."""
	names = frappe.db.sql_list(
		"""select se.name from `tabStock Entry` se where se.docstatus = 1 and se.purpose in ('Manufacture', 'Repack')
		and not exists (select 1 from `tabGenealogy Link` gl where gl.stock_entry = se.name)
		order by se.posting_date, se.posting_time, se.creation limit %(limit)s""", {"limit": int(limit or 10**9)})
	for name in names:
		on_stock_entry_submit(frappe.get_doc("Stock Entry", name))
	return len(names)


def on_stock_entry_cancel(doc, method=None) -> None:
	frappe.db.delete("Genealogy Link", {"stock_entry": doc.name})


def stamp_supplier_lots(doc, method=None) -> None:
	"""Purchase Receipt submit: remember which supplier (and supplier lot) a batch came from."""
	for row in doc.items:
		batch = row.get("batch_no")
		if batch:
			values = {"automotive_supplier": doc.supplier}
			lot = row.get("automotive_supplier_lot")
			if lot:
				values["automotive_supplier_lot"] = lot
			frappe.db.set_value("Batch", batch, values, update_modified=False)


# ---------------------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------------------


def components_of(serial_no: str, max_depth: int = 8) -> list[dict]:
	"""Upstream tree as a flat list of {level, parent, item, serial, batch, supplier, lot}."""
	result = []
	queue = deque([(serial_no, None, 0)])
	seen = set()
	while queue:
		serial, batch, depth = queue.popleft()
		if depth >= max_depth or (serial, batch) in seen:
			continue
		seen.add((serial, batch))
		filters = {"parent_serial_no": serial} if serial else {"parent_batch_no": batch}
		links = frappe.get_all("Genealogy Link", filters=filters,
			fields=["parent_item", "component_item", "component_serial_no", "component_batch_no", "qty", "supplier",
				"supplier_lot", "stock_entry"], order_by="component_item asc, component_serial_no asc")
		for link in links:
			result.append({"level": depth + 1, "parent": serial or batch, **link})
			if link.component_serial_no:
				queue.append((link.component_serial_no, None, depth + 1))
			elif link.component_batch_no:
				queue.append((None, link.component_batch_no, depth + 1))
	return result


def units_containing(item_code: str | None = None, batch_nos: list[str] | None = None,
		serial_nos: list[str] | None = None, supplier: str | None = None, max_depth: int = 8) -> list[dict]:
	"""Every top-level unit (serial with no further parent) built from the given components."""
	frontier = set()
	values: dict = {}
	clauses = []
	selectors = []
	if batch_nos:
		selectors.append("component_batch_no in %(batches)s")
		values["batches"] = tuple(batch_nos)
	if serial_nos:
		selectors.append("component_serial_no in %(serials)s")
		values["serials"] = tuple(serial_nos)
	if selectors:
		clauses.append("(" + " or ".join(selectors) + ")")
	if item_code:
		clauses.append("component_item = %(item)s")
		values["item"] = item_code
	if supplier:
		clauses.append("supplier = %(supplier)s")
		values["supplier"] = supplier
	if not clauses:
		raise ValueError("give a component item, batches, serials or a supplier")
	where = " and ".join(clauses)
	start = frappe.db.sql(
		f"""select parent_item, parent_serial_no, parent_batch_no, component_item,
		ifnull(component_serial_no, component_batch_no) as via from `tabGenealogy Link` where {where}""",  # noqa: S608 (constant SQL fragments; values are bound)
		values,
		as_dict=True,
	)
	paths = {}
	for row in start:
		key = (row.parent_serial_no, row.parent_batch_no)
		frontier.add(key)
		paths[key] = f"{row.component_item}:{row.via} -> {row.parent_item}:{row.parent_serial_no or row.parent_batch_no}"
	finished: dict[str, dict] = {}
	depth = 0
	while frontier and depth < max_depth:
		depth += 1
		next_frontier = set()
		for serial, batch in sorted(frontier, key=lambda k: (k[0] or "", k[1] or "")):
			field, value = ("component_serial_no", serial) if serial else ("component_batch_no", batch)
			parents = frappe.get_all("Genealogy Link", filters={field: value},
				fields=["parent_item", "parent_serial_no", "parent_batch_no"])
			if not parents:
				if serial:
					item = frappe.db.get_value("Serial No", serial, "item_code")
					finished[serial] = {"serial_no": serial, "item_code": item, "path": paths[(serial, batch)]}
				continue
			for p in parents:
				key = (p.parent_serial_no, p.parent_batch_no)
				if key not in paths:
					paths[key] = f"{paths[(serial, batch)]} -> {p.parent_item}:{p.parent_serial_no or p.parent_batch_no}"
				next_frontier.add(key)
		frontier = next_frontier
	return [finished[k] for k in sorted(finished)]


@frappe.whitelist()
def get_components(serial_no: str) -> list[dict]:
	frappe.has_permission("Genealogy Link", "read", throw=True)
	return components_of(serial_no)


@frappe.whitelist()
def get_affected_units(item_code: str, batch_nos: str | None = None, serial_nos: str | None = None) -> list[dict]:
	frappe.has_permission("Genealogy Link", "read", throw=True)
	split = lambda text: [v.strip() for v in (text or "").replace(",", "\n").splitlines() if v.strip()]  # noqa: E731
	return units_containing(item_code, split(batch_nos), split(serial_nos))
