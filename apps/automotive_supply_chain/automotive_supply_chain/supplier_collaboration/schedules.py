"""Supplier schedules, blanket-order call-offs and order acknowledgements (ERP-05, J-02).

* Open purchase Material Requests (MRP or Kanban) for a supplier's approved items are
  turned into a versioned Supplier Delivery Schedule; publishing it releases the firm
  lines as Blanket Call-Off purchase orders (no fresh approval: the blanket order is
  the approved commitment) and supersedes the previous version.
* Kanban requests are also released continuously (``release_kanban_calloffs``) so a
  pull signal reaches the supplier within minutes.
* Suppliers confirm or revise each PO line (Supplier Order Acknowledgement); the
  latest promise is kept on the PO item while every acknowledgement stays on record.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, now_datetime, nowdate

from automotive_core.governance import audit
from automotive_core.integration_hub import outbox
from automotive_core.utils import commit
from automotive_supply_chain.supplier_collaboration.sourcing import approved_entries


def _blanket_order_for(supplier: str, company: str, item_code: str, on_date) -> tuple[str, float] | None:
	row = frappe.db.sql(
		"""select bo.name, boi.rate from `tabBlanket Order` bo join `tabBlanket Order Item` boi on boi.parent = bo.name
		where bo.docstatus = 1 and bo.blanket_order_type = 'Purchasing' and bo.supplier = %s and bo.company = %s
		and boi.item_code = %s and bo.from_date <= %s and bo.to_date >= %s and boi.qty > boi.ordered_qty
		order by bo.from_date desc limit 1""",
		(supplier, company, item_code, on_date, on_date),
	)
	return (row[0][0], flt(row[0][1])) if row else None


def _open_requests(company: str, plant: str, supplier: str | None = None, kanban_only: bool = False) -> list[dict]:
	rows = frappe.db.sql(
		f"""select mr.name mr, mri.name mri, mri.item_code, (mri.stock_qty - mri.ordered_qty) qty, mri.schedule_date,
			mri.warehouse, mr.automotive_kanban_signal kanban_signal
		from `tabMaterial Request Item` mri join `tabMaterial Request` mr on mr.name = mri.parent
		where mr.docstatus = 1 and mr.material_request_type = 'Purchase' and mr.company = %(company)s
		and mr.status not in ('Stopped', 'Cancelled', 'Ordered') and mri.stock_qty > mri.ordered_qty
		{"and ifnull(mr.automotive_kanban_signal, '') != ''" if kanban_only else ""}
		order by mri.schedule_date, mr.name, mri.idx""",  # noqa: S608 (constant SQL fragments; values are bound)
		{"company": company},
		as_dict=True,
	)
	out = []
	for row in rows:
		if frappe.db.get_value("Warehouse", row.warehouse, "automotive_plant") != plant:
			continue
		source = supplier_for_request(row, plant)
		if source and (not supplier or source == supplier):
			out.append({**row, "supplier": source})
	return out


def supplier_for_request(row, plant: str) -> str | None:
	if row.get("kanban_signal"):
		loop = frappe.db.get_value("Kanban Signal", row["kanban_signal"], "kanban_loop")
		supplier = frappe.db.get_value("Kanban Loop", loop, "supplier") if loop else None
		if supplier:
			return supplier
	entries = approved_entries(row["item_code"], plant, row["schedule_date"])
	return entries[0].supplier if entries else None


def make_calloff(company: str, supplier: str, plant: str, lines: list[dict], schedule: str | None = None) -> str | None:
	po = frappe.new_doc("Purchase Order")
	po.update({"company": company, "supplier": supplier, "transaction_date": nowdate(),
		"automotive_release_type": "Blanket Call-Off", "automotive_plant": plant,
		"automotive_supplier_schedule": schedule})
	for line in lines:
		blanket = _blanket_order_for(supplier, company, line["item_code"], getdate(nowdate()))
		if not blanket:
			continue
		po.append("items", {
			"item_code": line["item_code"],
			"qty": flt(line["qty"]),
			"schedule_date": max(getdate(line["schedule_date"]), getdate(nowdate())),
			"warehouse": line["warehouse"],
			"rate": blanket[1],
			"blanket_order": blanket[0],
			"blanket_order_rate": blanket[1],
			"against_blanket_order": 1,
			"material_request": line.get("mr"),
			"material_request_item": line.get("mri"),
		})
	if not po.items:
		po.automotive_release_type = "Standard"
		return None
	po.schedule_date = min(getdate(i.schedule_date) for i in po.items)
	po.flags.ignore_permissions = True
	po.insert()
	po.submit()
	for line in lines:
		if line.get("kanban_signal"):
			frappe.db.set_value("Kanban Signal", line["kanban_signal"], {"purchase_order": po.name, "status": "Ordered"})
	return po.name


@frappe.whitelist(methods=["POST"])
def release_calloffs_now() -> list[str]:
	"""Buyer action: release open Kanban call-offs immediately instead of waiting for the
	five-minute scheduler run (same function, same rules)."""
	frappe.only_for(("Buyer", "Procurement Manager", "System Manager"))
	return release_kanban_calloffs()


def release_kanban_calloffs() -> list[str]:
	"""Every few minutes: one call-off PO per (company, plant, supplier) for open Kanban requests."""
	created = []
	for plant in frappe.get_all("Plant", filters={"is_active": 1}, fields=["name", "company"]):
		groups: dict[str, list[dict]] = defaultdict(list)
		for row in _open_requests(plant.company, plant.name, kanban_only=True):
			groups[row["supplier"]].append(row)
		for supplier, lines in sorted(groups.items()):
			po = make_calloff(plant.company, supplier, plant.name, lines)
			if po:
				created.append(po)
	commit()
	return created


# ---------------------------------------------------------------------------------------
# Supplier Delivery Schedule
# ---------------------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def generate(supplier: str, plant: str) -> str:
	frappe.only_for(("Buyer", "Procurement Manager", "System Manager"))
	settings = frappe.get_cached_doc("Supply Chain Settings")
	company = frappe.get_cached_value("Plant", plant, "company")
	start = getdate(nowdate())
	end = add_days(start, int(settings.schedule_horizon_days or 56))
	firm_days = int(settings.schedule_firm_days or 10)
	lines = [r for r in _open_requests(company, plant, supplier) if getdate(r["schedule_date"]) <= end]
	if not lines:
		frappe.throw(_("No open requirements for {0} at {1}.").format(supplier, plant))
	doc = frappe.get_doc({
		"doctype": "Supplier Delivery Schedule",
		"supplier": supplier,
		"company": company,
		"plant": plant,
		"horizon_start": start,
		"horizon_end": end,
		"firm_days": firm_days,
		"lines": [{
			"item_code": r["item_code"], "delivery_date": r["schedule_date"], "qty": r["qty"],
			"firm": int(getdate(r["schedule_date"]) <= add_days(start, firm_days)), "warehouse": r["warehouse"],
			"material_request": r["mr"],
		} for r in lines],
	})
	doc.insert()
	return doc.name


def before_insert_schedule(doc) -> None:
	latest = frappe.db.sql("""select max(schedule_version) from `tabSupplier Delivery Schedule`
		where supplier=%s and plant=%s""", (doc.supplier, doc.plant))[0][0]
	doc.schedule_version = int(latest or 0) + 1


def on_schedule_submit(doc) -> None:
	previous = frappe.get_all("Supplier Delivery Schedule", filters={"supplier": doc.supplier, "plant": doc.plant,
		"docstatus": 1, "name": ("!=", doc.name), "status": ("in", ("Published", "Acknowledged"))}, pluck="name")
	for name in previous:
		frappe.db.set_value("Supplier Delivery Schedule", name, {"status": "Superseded", "superseded_by": doc.name})
	firm = [{"item_code": ln.item_code, "qty": ln.qty, "schedule_date": ln.delivery_date, "warehouse": ln.warehouse,
		"mr": ln.material_request, "mri": frappe.db.get_value("Material Request Item",
			{"parent": ln.material_request, "item_code": ln.item_code}, "name") if ln.material_request else None,
		"kanban_signal": frappe.db.get_value("Material Request", ln.material_request, "automotive_kanban_signal")
			if ln.material_request else None}
		for ln in doc.lines if ln.firm and not ln.purchase_order]
	po = make_calloff(doc.company, doc.supplier, doc.plant, firm, schedule=doc.name) if firm else None
	if po:
		for ln in doc.lines:
			if ln.firm:
				ln.db_set("purchase_order", po)
	doc.db_set({"status": "Published", "published_at": now_datetime()})
	outbox.publish("supplier_schedule.published", {"schedule": doc.name, "version": doc.schedule_version,
		"lines": [{"item": ln.item_code, "date": str(ln.delivery_date), "qty": ln.qty, "firm": ln.firm}
			for ln in doc.lines]}, doc.doctype, doc.name, party_type="Supplier", party=doc.supplier)


@frappe.whitelist(methods=["POST"])
def acknowledge_schedule(name: str) -> str:
	from automotive_supply_chain.partners import assert_party_access

	doc = frappe.get_doc("Supplier Delivery Schedule", name)
	assert_party_access("Supplier", doc.supplier)
	if doc.status != "Published":
		frappe.throw(_("Schedule {0} is {1}.").format(name, doc.status))
	doc.db_set({"status": "Acknowledged", "acknowledged_at": now_datetime(), "acknowledged_by": frappe.session.user})
	return "Acknowledged"


# ---------------------------------------------------------------------------------------
# Order acknowledgements
# ---------------------------------------------------------------------------------------


def on_purchase_order_submit(doc, method=None) -> None:
	if frappe.db.get_value("Supplier", doc.supplier, "is_internal_supplier"):
		return
	doc.db_set("automotive_ack_status", "Awaiting")
	outbox.publish("purchase_order.submitted", {
		"purchase_order": doc.name, "supplier": doc.supplier, "company": doc.company,
		"release_type": doc.get("automotive_release_type"), "currency": doc.currency,
		"lines": [{"row": i.name, "item_code": i.item_code, "qty": i.qty, "uom": i.uom, "rate": i.rate,
			"schedule_date": str(i.schedule_date), "warehouse": i.warehouse} for i in doc.items],
	}, doc.doctype, doc.name, party_type="Supplier", party=doc.supplier)


def validate_acknowledgement(doc) -> None:
	po = frappe.get_doc("Purchase Order", doc.purchase_order)
	if po.docstatus != 1:
		frappe.throw(_("Purchase Order {0} is not open.").format(po.name))
	doc.supplier = po.supplier
	rows = {i.name: i for i in po.items}
	if not doc.lines and doc.ack_type == "Accepted":
		for item in po.items:
			doc.append("lines", {"po_item": item.name, "item_code": item.item_code, "ordered_qty": item.qty,
				"confirmed_qty": item.qty, "promised_date": item.schedule_date})
	for line in doc.lines:
		row = rows.get(line.po_item)
		if not row:
			frappe.throw(_("Line {0}: {1} is not a row of {2}.").format(line.idx, line.po_item, po.name))
		line.item_code, line.ordered_qty = row.item_code, row.qty
		if flt(line.confirmed_qty) < 0 or flt(line.confirmed_qty) > flt(row.qty):
			frappe.throw(_("Line {0}: confirmed quantity must be between 0 and {1}.").format(line.idx, row.qty))
	changed = any(flt(ln.confirmed_qty) != flt(ln.ordered_qty)
		or getdate(ln.promised_date) != getdate(rows[ln.po_item].schedule_date) for ln in doc.lines)
	if doc.ack_type == "Accepted" and changed:
		doc.ack_type = "Accepted With Changes"  # a revised quantity or delivery promise
	if doc.is_new():
		doc.acknowledged_by = frappe.session.user
		doc.acknowledged_at = now_datetime()


def on_acknowledgement_submit(doc) -> None:
	for line in doc.lines:
		frappe.db.set_value("Purchase Order Item", line.po_item, "automotive_promised_date", line.promised_date,
			update_modified=False)
	frappe.db.set_value("Purchase Order", doc.purchase_order, {"automotive_ack_status": doc.ack_type,
		"automotive_ack_at": doc.acknowledged_at})
	audit.record("supplier.po_acknowledged", "Purchase Order", doc.purchase_order,
		{"acknowledgement": doc.name, "type": doc.ack_type})
