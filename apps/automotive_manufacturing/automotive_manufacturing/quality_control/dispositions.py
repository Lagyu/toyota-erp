"""Quality Disposition execution (ERP-07, J-05): the approved decision becomes the
stock movement, traceable to the disposition (automotive_source_* on the entry)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from automotive_core.governance import audit
from automotive_core.governance.elevation import on_behalf_of
from automotive_manufacturing.quality_control.holds import close_dispositioned


def _plant_warehouse(plant: str | None, field: str) -> str | None:
	return frappe.db.get_value("Plant", plant, field) if plant else None


def validate(doc) -> None:
	if frappe.get_cached_value("Warehouse", doc.from_warehouse, "automotive_warehouse_role") not in ("Quarantine",
			"Receiving Inspection"):
		frappe.throw(_("Dispositions act on segregated stock: {0} is not a quarantine/inspection warehouse.").format(
			doc.from_warehouse))
	if doc.decision in ("Release", "Use As Is", "Rework") and not doc.to_warehouse:
		frappe.throw(_("Choose the destination warehouse."))
	if doc.decision == "Return to Supplier" and not doc.supplier:
		doc.supplier = frappe.db.get_value("Batch", doc.batch_no, "automotive_supplier") if doc.batch_no else None
		if not doc.supplier:
			frappe.throw(_("Choose the supplier to return to."))
	if not doc.plant:
		doc.plant = frappe.db.get_value("Warehouse", doc.from_warehouse, "automotive_plant")


def _stock_entry(doc, purpose: str, target: str | None):
	se = frappe.new_doc("Stock Entry")
	se.purpose = purpose
	se.stock_entry_type = purpose
	se.company = doc.company
	se.automotive_source_doctype = doc.doctype
	se.automotive_source_name = doc.name
	se.remarks = _("Quality disposition {0}: {1}").format(doc.name, doc.decision)
	se.append("items", {
		"item_code": doc.item_code,
		"qty": flt(doc.qty),
		"s_warehouse": doc.from_warehouse,
		"t_warehouse": target,
		"batch_no": doc.batch_no,
		"serial_no": doc.serial_nos,
		"use_serial_batch_fields": 1,
	})
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se.name


def _purchase_return(doc) -> str:
	from erpnext.controllers.sales_and_purchase_return import make_return_doc

	receipt = frappe.db.sql(
		"""select pr.name from `tabPurchase Receipt` pr join `tabPurchase Receipt Item` pri on pri.parent = pr.name
		where pr.docstatus = 1 and pr.is_return = 0 and pr.supplier = %s and pri.item_code = %s
		and (%s is null or pri.batch_no = %s) order by pr.posting_date desc limit 1""",
		(doc.supplier, doc.item_code, doc.batch_no, doc.batch_no),
	)
	if not receipt:
		frappe.throw(_("No purchase receipt from {0} for {1} to return against.").format(doc.supplier, doc.item_code))
	# the approved disposition is the authority for the return; quality staff need not
	# hold purchasing rights, so the return is a system step recorded on their behalf
	with on_behalf_of(_("Return for disposition {0}").format(doc.name), doc.doctype, doc.name) as requester:
		ret = make_return_doc("Purchase Receipt", receipt[0][0])
		ret.items = [r for r in ret.items if r.item_code == doc.item_code
			and (not doc.batch_no or r.batch_no == doc.batch_no)]
		for row in ret.items:
			row.qty = -flt(doc.qty)
			row.received_qty = -flt(doc.qty)
			row.warehouse = doc.from_warehouse
			row.use_serial_batch_fields = 1
			row.batch_no = doc.batch_no
		ret.insert()
		ret.submit()
		ret.db_set("owner", requester, update_modified=False)
	return ret.name


def execute(doc) -> None:
	if doc.decision in ("Release", "Use As Is", "Rework"):
		doc.db_set("stock_entry", _stock_entry(doc, "Material Transfer", doc.to_warehouse))
	elif doc.decision == "Scrap":
		scrap = _plant_warehouse(doc.plant, "scrap_warehouse")
		if scrap:
			moved = _stock_entry(doc, "Material Transfer", scrap)
			doc.db_set("stock_entry", moved)
		else:
			doc.db_set("stock_entry", _stock_entry(doc, "Material Issue", None))
	elif doc.decision == "Return to Supplier":
		doc.db_set("purchase_return", _purchase_return(doc))
	if doc.quality_hold and _hold_fully_dispositioned(doc.quality_hold):
		close_dispositioned(doc.quality_hold)
	audit.record("quality.disposition_executed", doc.doctype, doc.name,
		{"decision": doc.decision, "item": doc.item_code, "batch": doc.batch_no, "qty": flt(doc.qty),
		 "stock_entry": doc.stock_entry, "purchase_return": doc.purchase_return}, company=doc.company)


def _hold_fully_dispositioned(hold: str) -> bool:
	"""All held stock left segregation (nothing of the held batch/serial remains in quarantine)."""
	h = frappe.db.get_value("Quality Hold", hold, ["hold_scope", "item", "batch_no", "serial_no"], as_dict=True)
	if h.hold_scope == "Batch":
		remaining = frappe.db.sql(
			"""select ifnull(sum(sbe.qty), 0) from `tabSerial and Batch Entry` sbe
			join `tabSerial and Batch Bundle` sbb on sbb.name = sbe.parent
			join `tabWarehouse` w on w.name = sbb.warehouse
			where sbe.batch_no = %s and sbb.docstatus = 1 and sbb.is_cancelled = 0
			and w.automotive_warehouse_role in ('Quarantine', 'Receiving Inspection')""",
			h.batch_no,
		)[0][0]
		return flt(remaining) <= 0
	if h.hold_scope == "Serial No":
		warehouse = frappe.db.get_value("Serial No", h.serial_no, "warehouse")
		role = frappe.db.get_value("Warehouse", warehouse, "automotive_warehouse_role") if warehouse else None
		return role not in ("Quarantine", "Receiving Inspection")
	return False
