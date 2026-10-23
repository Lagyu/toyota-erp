"""Three-way match: supplier invoice vs purchase order vs accepted receipt (ERP-05/09, J-06).

Every invoice line is checked against its order line (price) and its receipt line
(cumulative billed quantity <= accepted quantity). Lines outside the tolerances in
Supply Chain Settings become Invoice Match Exceptions; an invoice with open
exceptions cannot be submitted, so no payable exists and no payment can be made.
An exception is either accepted (override by a procurement manager or controller who
did not enter the invoice) or the invoice is corrected (the exception then closes).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from automotive_core.governance import audit


class MatchExceptionError(frappe.ValidationError):
	pass


def _accepted_qty(pr_detail: str) -> float:
	row = frappe.db.get_value("Purchase Receipt Item", pr_detail, ["qty", "returned_qty", "docstatus"], as_dict=True)
	if not row or row.docstatus != 1:
		return 0.0
	return flt(row.qty) - flt(row.returned_qty)


def _billed_before(pr_detail: str, exclude: str) -> float:
	return flt(frappe.db.sql(
		"""select sum(qty) from `tabPurchase Invoice Item` where pr_detail = %s and docstatus = 1 and parent != %s""",
		(pr_detail, exclude))[0][0])


def check_intercompany(doc) -> list[dict]:
	"""An intercompany invoice must mirror the seller's submitted Sales Invoice line by line."""
	found = []
	reference = doc.get("inter_company_invoice_reference")
	mirror = frappe.get_doc("Sales Invoice", reference) if reference and frappe.db.exists("Sales Invoice", reference) else None
	if not mirror or mirror.docstatus != 1:
		return [{"pi_item": row.name, "item_code": row.item_code, "exception_type": "No Order"} for row in doc.items]
	seller_lines: dict[str, list] = {}
	for line in mirror.items:
		seller_lines.setdefault(line.item_code, []).append(line)
	for row in doc.items:
		candidates = seller_lines.get(row.item_code) or []
		match = next((c for c in candidates if abs(flt(c.qty) - flt(row.qty)) < 1e-9), None)
		if not match:
			found.append({"pi_item": row.name, "item_code": row.item_code, "exception_type": "Quantity Over Accepted",
				"invoiced_qty": flt(row.qty), "accepted_qty": sum(flt(c.qty) for c in candidates)})
		elif abs(flt(match.rate) - flt(row.rate)) > 0.005:
			found.append({"pi_item": row.name, "item_code": row.item_code, "exception_type": "Price Variance",
				"invoice_rate": row.rate, "order_rate": match.rate,
				"variance_amount": (flt(row.rate) - flt(match.rate)) * flt(row.qty)})
	return found


def check(doc) -> list[dict]:
	if frappe.db.get_value("Supplier", doc.supplier, "is_internal_supplier"):
		return check_intercompany(doc)
	settings = frappe.get_cached_doc("Supply Chain Settings")
	qty_tol, pct_tol, amt_tol = (flt(settings.qty_tolerance_percent), flt(settings.price_tolerance_percent),
		flt(settings.price_tolerance_amount))
	found = []
	for row in doc.items:
		base = {"pi_item": row.name, "item_code": row.item_code, "purchase_order": row.purchase_order,
			"purchase_receipt": row.purchase_receipt}
		if not row.purchase_order:
			found.append({**base, "exception_type": "No Order"})
			continue
		is_stock = frappe.get_cached_value("Item", row.item_code, "is_stock_item")
		if is_stock and not row.pr_detail:
			found.append({**base, "exception_type": "No Receipt"})
		order_rate = flt(frappe.db.get_value("Purchase Order Item", row.po_detail, "rate"))
		diff = flt(row.rate) - order_rate
		pct = diff / order_rate * 100 if order_rate else 0
		if diff > 0 and not (pct <= pct_tol or diff <= amt_tol):
			found.append({**base, "exception_type": "Price Variance", "invoice_rate": row.rate, "order_rate": order_rate,
				"variance_amount": diff * flt(row.qty), "variance_percent": pct})
		if row.pr_detail:
			accepted = _accepted_qty(row.pr_detail)
			billed = _billed_before(row.pr_detail, doc.name or "") + flt(row.qty)
			if billed > accepted * (1 + qty_tol / 100) + 1e-9:
				found.append({**base, "exception_type": "Quantity Over Accepted", "invoiced_qty": billed,
					"accepted_qty": accepted, "variance_amount": (billed - accepted) * flt(row.rate)})
	return found


def sync_exceptions(doc) -> list[str]:
	found = check(doc)
	keys = {(f["pi_item"], f["exception_type"]) for f in found}
	existing = frappe.get_all("Invoice Match Exception", filters={"purchase_invoice": doc.name},
		fields=["name", "pi_item", "exception_type", "status"])
	for row in existing:
		if (row.pi_item, row.exception_type) not in keys and row.status in ("Open", "Rejected"):
			frappe.db.set_value("Invoice Match Exception", row.name, {"status": "Corrected",
				"resolution_note": _("Invoice corrected"), "resolved_at": now_datetime()})
	have = {(r.pi_item, r.exception_type): r for r in existing}
	open_names = []
	for item in found:
		key = (item["pi_item"], item["exception_type"])
		if key in have and have[key].status in ("Open", "Accepted", "Rejected"):
			current = have[key]
			if current.status == "Open":
				frappe.db.set_value("Invoice Match Exception", current.name,
					{k: v for k, v in item.items() if k not in ("pi_item", "exception_type")})
			if current.status in ("Open", "Rejected"):
				open_names.append(current.name)  # a rejected line blocks until the invoice is corrected
			continue
		exc = frappe.get_doc({"doctype": "Invoice Match Exception", "purchase_invoice": doc.name,
			"supplier": doc.supplier, "company": doc.company, "status": "Open", **item})
		exc.flags.ignore_permissions = True
		exc.insert()
		open_names.append(exc.name)
	return open_names


def validate_purchase_invoice(doc, method=None) -> None:
	if doc.get("is_return") or doc.docstatus == 2 or doc.is_new():
		return
	open_names = sync_exceptions(doc)
	accepted = frappe.db.exists("Invoice Match Exception", {"purchase_invoice": doc.name, "status": "Accepted"})
	doc.automotive_match_status = "Exceptions Open" if open_names else ("Exceptions Accepted" if accepted else "Matched")


def after_insert_purchase_invoice(doc, method=None) -> None:
	if doc.get("is_return"):
		return
	open_names = sync_exceptions(doc)
	doc.db_set("automotive_match_status", "Exceptions Open" if open_names else "Matched", update_modified=False)


def before_submit_purchase_invoice(doc, method=None) -> None:
	if doc.get("is_return"):
		return
	open_names = sync_exceptions(doc)
	if open_names:
		raise MatchExceptionError(_("Three-way match exceptions are open: {0}").format(", ".join(open_names)))


def _resolve(name: str, status: str, note: str) -> str:
	frappe.only_for(("Procurement Manager", "Group Controller", "System Manager"))
	exc = frappe.get_doc("Invoice Match Exception", name)
	if exc.status != "Open":
		frappe.throw(_("{0} is {1}.").format(name, exc.status))
	if not (note or "").strip():
		frappe.throw(_("A resolution note is required."))
	if frappe.session.user == frappe.db.get_value("Purchase Invoice", exc.purchase_invoice, "owner"):
		raise frappe.PermissionError(_("The person who entered the invoice cannot resolve its exceptions."))
	exc.update({"status": status, "resolution_note": note, "resolved_by": frappe.session.user,
		"resolved_at": now_datetime()})
	exc.flags.ignore_permissions = True
	exc.save()
	audit.record(f"three_way_match.{status.lower()}", exc.doctype, exc.name,
		{"invoice": exc.purchase_invoice, "type": exc.exception_type, "variance": flt(exc.variance_amount), "note": note},
		company=exc.company)
	return status


@frappe.whitelist(methods=["POST"])
def accept_exception(name: str, note: str) -> str:
	"""Accept a price variance: the open order line is re-priced to the accepted price (an
	audited order change), so ERPNext's over-billing control and the PPV both stay correct.
	Quantity exceptions cannot be accepted: invoice only what was accepted into stock."""
	exc = frappe.get_doc("Invoice Match Exception", name)
	if exc.exception_type != "Price Variance":
		frappe.throw(_("{0} exceptions cannot be accepted; correct the invoice or receive the balance.").format(
			_(exc.exception_type)))
	status = _resolve(name, "Accepted", note)
	if exc.purchase_order and not frappe.db.get_value("Supplier", exc.supplier, "is_internal_supplier"):
		_reprice_order_line(exc)
	return status


def _reprice_order_line(exc) -> None:
	import json

	from erpnext.controllers.accounts_controller import update_child_qty_rate

	po = frappe.get_doc("Purchase Order", exc.purchase_order)
	po_detail = frappe.db.get_value("Purchase Invoice Item", exc.pi_item, "po_detail")
	rows = []
	for item in po.items:
		rate = flt(exc.invoice_rate) if item.name == po_detail else item.rate
		rows.append({"docname": item.name, "item_code": item.item_code, "qty": item.qty, "rate": rate,
			"schedule_date": str(item.schedule_date), "conversion_factor": item.conversion_factor, "uom": item.uom})
	frappe.flags.ignore_permissions = True
	try:
		update_child_qty_rate("Purchase Order", json.dumps(rows), po.name)
	finally:
		frappe.flags.ignore_permissions = False
	audit.record("purchase_order.repriced", "Purchase Order", po.name, {"exception": exc.name, "row": po_detail,
		"from": flt(exc.order_rate), "to": flt(exc.invoice_rate)}, company=exc.company)


@frappe.whitelist(methods=["POST"])
def reject_exception(name: str, note: str) -> str:
	return _resolve(name, "Rejected", note)
