"""Supplier consignment / vendor-managed inventory (ERP-06).

The supplier's parts sit in the plant's consignment warehouse but still belong to the
supplier, so they are carried at zero value. Ownership — and the purchase obligation —
passes when the plant withdraws parts: a withdrawal issues them out of consignment
(zero value), raises a call-off purchase order against the agreement's blanket order
(the commercial contract; no new approval, like Kanban call-offs) and receives it into
the target warehouse at the agreed price. The supplier then invoices that receipt
through the normal three-way match. Returns go back to the supplier with no financial
effect. Every step is an ordinary ERPNext document, linked from the movement.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from automotive_core.governance import audit


def _blanket_rates(blanket_order: str) -> dict[str, float]:
	return {
		row.item_code: flt(row.rate)
		for row in frappe.get_all(
			"Blanket Order Item", filters={"parent": blanket_order}, fields=["item_code", "rate"]
		)
	}


def validate_agreement(doc) -> None:
	if getdate(doc.valid_to) < getdate(doc.valid_from):
		frappe.throw(_("Valid To must be on or after Valid From."))
	warehouse = frappe.db.get_value(
		"Warehouse",
		doc.consignment_warehouse,
		["company", "automotive_warehouse_role", "automotive_plant"],
		as_dict=True,
	)
	if (
		not warehouse
		or warehouse.company != doc.company
		or warehouse.automotive_warehouse_role != "Consignment"
	):
		frappe.throw(
			_("{0} is not a consignment warehouse of {1}.").format(doc.consignment_warehouse, doc.company)
		)
	if warehouse.automotive_plant and warehouse.automotive_plant != doc.plant:
		frappe.throw(
			_("{0} belongs to plant {1}.").format(doc.consignment_warehouse, warehouse.automotive_plant)
		)
	blanket = frappe.db.get_value(
		"Blanket Order",
		doc.blanket_order,
		["supplier", "company", "docstatus", "blanket_order_type"],
		as_dict=True,
	)
	if (
		not blanket
		or blanket.docstatus != 1
		or blanket.supplier != doc.supplier
		or blanket.company != doc.company
		or blanket.blanket_order_type != "Purchasing"
	):
		frappe.throw(
			_("{0} is not a submitted purchasing blanket order of {1} with {2}.").format(
				doc.blanket_order, doc.company, doc.supplier
			)
		)
	rates = _blanket_rates(doc.blanket_order)
	for row in doc.items:
		if row.item_code not in rates:
			frappe.throw(
				_("Row {0}: {1} is not on blanket order {2}.").format(
					row.idx, row.item_code, doc.blanket_order
				)
			)
		if flt(row.max_qty) and flt(row.min_qty) > flt(row.max_qty):
			frappe.throw(_("Row {0}: minimum exceeds maximum.").format(row.idx))
		row.agreed_rate = rates[row.item_code]
	if doc.docstatus == 0:
		doc.status = "Draft"


def on_agreement_submit(doc) -> None:
	doc.db_set("status", "Active")
	audit.record(
		"consignment.agreement_active",
		doc.doctype,
		doc.name,
		{"supplier": doc.supplier, "items": [r.item_code for r in doc.items]},
		company=doc.company,
	)


def on_agreement_cancel(doc) -> None:
	doc.db_set("status", "Terminated")


def on_hand(agreement: str, item_code: str) -> float:
	warehouse = frappe.db.get_value("Consignment Agreement", agreement, "consignment_warehouse")
	return flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))


def validate_movement(doc) -> None:
	agreement = frappe.get_doc("Consignment Agreement", doc.agreement)
	if agreement.docstatus != 1 or agreement.status != "Active":
		frappe.throw(_("Consignment agreement {0} is not active.").format(agreement.name))
	day = getdate(doc.posting_date or nowdate())
	if not (getdate(agreement.valid_from) <= day <= getdate(agreement.valid_to)):
		frappe.throw(_("Agreement {0} is not valid on {1}.").format(agreement.name, day))
	doc.supplier, doc.company = agreement.supplier, agreement.company
	allowed = {r.item_code: r for r in agreement.items}
	for row in doc.items:
		term = allowed.get(row.item_code)
		if not term:
			frappe.throw(
				_("Row {0}: {1} is not covered by {2}.").format(row.idx, row.item_code, agreement.name)
			)
		if doc.movement_type == "Receipt Into Consignment":
			if (
				flt(term.max_qty)
				and on_hand(agreement.name, row.item_code) + flt(row.qty) > flt(term.max_qty) + 1e-9
			):
				frappe.throw(
					_("Row {0}: {1} would exceed the agreed maximum of {2}.").format(
						row.idx, row.item_code, term.max_qty
					)
				)
		else:
			if flt(row.qty) > on_hand(agreement.name, row.item_code) + 1e-9:
				frappe.throw(
					_("Row {0}: only {1} of {2} on consignment.").format(
						row.idx, on_hand(agreement.name, row.item_code), row.item_code
					)
				)
			if doc.movement_type == "Withdrawal" and not row.target_warehouse:
				frappe.throw(_("Row {0}: choose the warehouse the parts are withdrawn to.").format(row.idx))
			if (
				row.target_warehouse
				and frappe.db.get_value("Warehouse", row.target_warehouse, "company") != doc.company
			):
				frappe.throw(
					_("Row {0}: {1} is not a warehouse of {2}.").format(
						row.idx, row.target_warehouse, doc.company
					)
				)


def _stock_entry(doc, agreement, purpose: str) -> str:
	"""Zero-value move in or out of the consignment warehouse (the stock is not ours)."""
	se = frappe.get_doc(
		{
			"doctype": "Stock Entry",
			"purpose": purpose,
			"stock_entry_type": purpose,
			"company": doc.company,
			"posting_date": doc.posting_date,
			"set_posting_time": 1,
			"remarks": _("Consignment {0} ({1})").format(doc.name, doc.movement_type),
			"items": [
				{
					"item_code": row.item_code,
					"qty": row.qty,
					"use_serial_batch_fields": 1,
					"batch_no": row.batch_no,
					"s_warehouse": agreement.consignment_warehouse if purpose == "Material Issue" else None,
					"t_warehouse": agreement.consignment_warehouse if purpose == "Material Receipt" else None,
					"basic_rate": 0,
					"allow_zero_valuation_rate": 1,
				}
				for row in doc.items
			],
		}
	)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se.name


def _take_ownership(doc, agreement) -> tuple[str, str]:
	from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

	from automotive_supply_chain.supplier_collaboration.schedules import make_calloff

	lines = [
		{
			"item_code": row.item_code,
			"qty": row.qty,
			"schedule_date": doc.posting_date,
			"warehouse": row.target_warehouse,
		}
		for row in doc.items
	]
	po_name = make_calloff(doc.company, doc.supplier, agreement.plant, lines)
	if not po_name:
		frappe.throw(_("No blanket-order price for the withdrawn items."))
	frappe.db.set_value(
		"Purchase Order", po_name, "automotive_ack_status", "Not Requested"
	)  # consumed, not ordered
	pr = make_purchase_receipt(po_name)
	pr.posting_date = doc.posting_date
	pr.set_posting_time = 1
	batches = {row.item_code: row.batch_no for row in doc.items if row.batch_no}
	for item in pr.items:
		item.use_serial_batch_fields = 1
		if batches.get(item.item_code):
			item.batch_no = batches[item.item_code]
	pr.supplier_delivery_note = doc.name
	pr.flags.ignore_permissions = True
	pr.insert()
	pr.submit()
	return po_name, pr.name


def on_movement_submit(doc) -> None:
	agreement = frappe.get_doc("Consignment Agreement", doc.agreement)
	if doc.movement_type == "Receipt Into Consignment":
		doc.db_set("stock_entry", _stock_entry(doc, agreement, "Material Receipt"))
	elif doc.movement_type == "Return To Supplier":
		doc.db_set("stock_entry", _stock_entry(doc, agreement, "Material Issue"))
	else:
		stock_entry = _stock_entry(doc, agreement, "Material Issue")
		po_name, pr_name = _take_ownership(doc, agreement)
		doc.db_set({"stock_entry": stock_entry, "purchase_order": po_name, "purchase_receipt": pr_name})
	audit.record(
		"consignment." + frappe.scrub(doc.movement_type),
		doc.doctype,
		doc.name,
		{"agreement": doc.agreement, "lines": [{"item": r.item_code, "qty": flt(r.qty)} for r in doc.items]},
		company=doc.company,
	)


def on_movement_cancel(doc) -> None:
	from automotive_core.governance.cancellation import cancel_linked

	reason = _("Consignment movement {0} cancelled").format(doc.name)
	for doctype, name in (
		("Purchase Receipt", doc.purchase_receipt),
		("Purchase Order", doc.purchase_order),
		("Stock Entry", doc.stock_entry),
	):
		if name and frappe.db.get_value(doctype, name, "docstatus") == 1:
			cancel_linked(doctype, name, reason)
