"""Advance shipping notices, receipt from ASN and receipt discrepancies (ERP-05/06, J-02)."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate, now_datetime, nowdate

from automotive_core.governance import audit


def validate_asn(doc) -> None:
	from automotive_supply_chain.partners import assert_party_access

	assert_party_access("Supplier", doc.supplier)
	dup = frappe.db.get_value("Advance Shipping Notice", {"supplier": doc.supplier, "supplier_asn_no": doc.supplier_asn_no,
		"docstatus": ("<", 2), "name": ("!=", doc.name)}, "name")
	if dup:
		frappe.throw(_("ASN number {0} was already sent ({1}).").format(doc.supplier_asn_no, dup))
	if getdate(doc.expected_arrival) < getdate(doc.ship_date):
		frappe.throw(_("Expected arrival is before the ship date."))
	plants = set()
	for row in doc.items:
		po_item = frappe.db.get_value("Purchase Order Item", row.po_item, ["parent", "item_code", "qty", "received_qty",
			"warehouse"], as_dict=True)
		if not po_item or po_item.parent != row.purchase_order:
			frappe.throw(_("Row {0}: {1} is not a line of {2}.").format(row.idx, row.po_item, row.purchase_order))
		po = frappe.db.get_value("Purchase Order", row.purchase_order, ["supplier", "docstatus", "company", "status"],
			as_dict=True)
		if po.supplier != doc.supplier or po.docstatus != 1 or po.status in ("Closed", "Completed"):
			frappe.throw(_("Row {0}: {1} is not an open order of {2}.").format(row.idx, row.purchase_order, doc.supplier))
		if po.status == "On Hold":
			# ERPNext refuses receipts against a held order; refuse the shipment notice too
			frappe.throw(_("Row {0}: {1} is on hold; ship once the buyer releases it.").format(row.idx,
				row.purchase_order))
		if po_item.item_code != row.item_code:
			frappe.throw(_("Row {0}: item differs from the order line ({1}).").format(row.idx, po_item.item_code))
		shipped_before = flt(frappe.db.sql(
			"""select sum(ai.qty) from `tabASN Item` ai join `tabAdvance Shipping Notice` a on a.name = ai.parent
			where a.docstatus = 1 and ai.po_item = %s and a.name != %s""", (row.po_item, doc.name or ""))[0][0])
		if shipped_before + flt(row.qty) > flt(po_item.qty) * 1.0001:
			frappe.throw(_("Row {0}: shipping {1} would exceed the ordered {2} (already announced {3}).").format(
				row.idx, row.qty, po_item.qty, shipped_before))
		doc.company = po.company
		plants.add(frappe.db.get_value("Warehouse", po_item.warehouse, "automotive_plant"))
	if len(plants) == 1:
		doc.plant = plants.pop()
	if doc.docstatus == 0:
		doc.status = "Draft"


def on_asn_submit(doc) -> None:
	doc.db_set("status", "In Transit")
	for row in doc.items:
		if row.kanban_signal:
			frappe.db.set_value("Kanban Signal", row.kanban_signal, "status", "Shipped")
	audit.record("supplier.asn_submitted", doc.doctype, doc.name, {"supplier": doc.supplier,
		"lines": len(doc.items)}, company=doc.company)


@frappe.whitelist(methods=["POST"])
def make_purchase_receipt(asn: str) -> str:
	"""Warehouse receiving: a draft Purchase Receipt pre-filled from the ASN (lots -> batches)."""
	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt as from_po

	from automotive_supply_chain.logistics.storage import putaway_applies

	doc = frappe.get_doc("Advance Shipping Notice", asn)
	frappe.has_permission("Purchase Receipt", "create", throw=True)
	if doc.docstatus != 1 or doc.status in ("Received", "Cancelled"):
		frappe.throw(_("ASN {0} is {1}.").format(asn, doc.status))
	pr = None
	for po_name in sorted({r.purchase_order for r in doc.items}):
		pr = from_po(po_name, target_doc=pr)
	wanted = {r.po_item: r for r in doc.items}
	pr.items = [i for i in pr.items if i.purchase_order_item in wanted]
	for item in pr.items:
		row = wanted[item.purchase_order_item]
		remaining = flt(row.qty) - flt(row.received_qty)
		item.qty = remaining
		item.received_qty = remaining
		item.automotive_asn_item = row.name
		item.automotive_supplier_lot = row.supplier_lot
		item.use_serial_batch_fields = 1
		if frappe.get_cached_value("Item", item.item_code, "has_batch_no"):
			item.batch_no = _batch_for(item.item_code, doc.supplier, row.supplier_lot)
		if row.serial_nos:
			item.serial_no = row.serial_nos
	pr.automotive_asn = doc.name
	pr.posting_date = nowdate()
	# into a forward store: put away up to each part's capacity, the rest to reserve storage
	pr.apply_putaway_rule = int(putaway_applies(pr))
	pr.flags.ignore_permissions = True
	pr.insert()
	if doc.status == "In Transit":
		doc.db_set("status", "Arrived")
	return pr.name


def _batch_for(item_code: str, supplier: str, supplier_lot: str | None) -> str:
	if supplier_lot:
		existing = frappe.db.get_value("Batch", {"item": item_code, "automotive_supplier": supplier,
			"automotive_supplier_lot": supplier_lot}, "name")
		if existing:
			return existing
	batch = frappe.get_doc({"doctype": "Batch", "item": item_code, "automotive_supplier": supplier,
		"automotive_supplier_lot": supplier_lot})
	batch.flags.ignore_permissions = True
	batch.insert()
	return batch.name


def on_purchase_receipt_submit(doc, method=None) -> None:
	if not doc.get("automotive_asn") or doc.get("is_return"):
		return
	asn = frappe.get_doc("Advance Shipping Notice", doc.automotive_asn)
	# per ASN line: put-away can split one announced line over several receipt lines
	received_lines: dict[str, list[float]] = {}
	for item in doc.items:
		if item.get("automotive_asn_item"):
			line = received_lines.setdefault(item.automotive_asn_item, [0.0, 0.0])
			line[0] += flt(item.qty)
			line[1] += flt(item.rejected_qty)
	for row in asn.items:
		if row.name not in received_lines:
			continue
		accepted, rejected = received_lines[row.name]
		frappe.db.set_value("ASN Item", row.name, "received_qty", flt(row.received_qty) + accepted)
		if abs(accepted + rejected - flt(row.qty)) > 1e-9 or rejected:
			kind = "Damaged" if rejected else ("Shortage" if accepted < flt(row.qty) else "Overage")
			disc = frappe.get_doc({"doctype": "Receipt Discrepancy", "supplier": doc.supplier,
				"advance_shipping_notice": asn.name, "purchase_receipt": doc.name, "item_code": row.item_code,
				"discrepancy_type": kind, "expected_qty": row.qty, "received_qty": accepted, "rejected_qty": rejected,
				"status": "Open"})
			disc.flags.ignore_permissions = True
			disc.insert()
			frappe.get_doc({"doctype": "Supplier Notice", "supplier": doc.supplier,
				"subject": _("{0} on ASN {1} / {2}").format(kind, asn.supplier_asn_no, row.item_code),
				"notice_type": "Receipt Discrepancy", "reference_doctype": "Receipt Discrepancy",
				"reference_name": disc.name, "message": _("Announced {0}, received {1}, rejected {2}.").format(
					row.qty, accepted, rejected)}).insert(ignore_permissions=True)
	asn.reload()
	fully = all(flt(r.received_qty) >= flt(r.qty) for r in asn.items)
	receipts = sorted({*(asn.purchase_receipts or "").split("\n"), doc.name} - {""})
	asn.db_set({"status": "Received" if fully else "Partially Received", "purchase_receipts": "\n".join(receipts)})


def on_purchase_receipt_cancel(doc, method=None) -> None:
	if not doc.get("automotive_asn"):
		return
	for item in doc.items:
		if item.get("automotive_asn_item"):
			current = flt(frappe.db.get_value("ASN Item", item.automotive_asn_item, "received_qty"))
			frappe.db.set_value("ASN Item", item.automotive_asn_item, "received_qty", max(current - flt(item.qty), 0))
	frappe.db.set_value("Advance Shipping Notice", doc.automotive_asn, "status", "Partially Received")


@frappe.whitelist(methods=["POST"])
def resolve_discrepancy(name: str, resolution: str, note: str) -> str:
	frappe.only_for(("Buyer", "Procurement Manager", "Warehouse Clerk", "System Manager"))
	doc = frappe.get_doc("Receipt Discrepancy", name)
	if doc.status != "Open":
		frappe.throw(_("{0} is already resolved.").format(name))
	doc.update({"status": "Resolved", "resolution": resolution, "resolution_note": note,
		"resolved_by": frappe.session.user, "resolved_at": now_datetime()})
	doc.save(ignore_permissions=True)
	return doc.status
