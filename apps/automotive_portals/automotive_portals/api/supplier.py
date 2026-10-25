"""Supplier portal API (ERP-05): a supplier user sees and acts only on their own
organisation's RFQs, orders, schedules, ASNs, discrepancies, notices and remittances.

Every function resolves the caller's supplier from their active Partner User Access and
filters by it on the server; nothing from the client selects the party.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt, now_datetime

from automotive_core.governance import elevation
from automotive_core.integration_hub.idempotency import idempotent
from automotive_core.utils import json_argument
from automotive_supply_chain.partners import capabilities, require_capability, require_party

PAGE = 50


def _supplier(capability: str | None = None) -> str:
	"""The caller's own supplier (and, with ``capability``, that their portal role allows it)."""
	return require_capability("Supplier", capability) if capability else require_party("Supplier")


def _owned(doctype: str, name: str, supplier: str, field: str = "supplier"):
	if frappe.db.get_value(doctype, name, field) != supplier:
		raise frappe.PermissionError(_("Not found."))
	return frappe.get_doc(doctype, name)


@frappe.whitelist()
def dashboard() -> dict[str, Any]:
	"""Counters for the overview, only those the caller's portal role may see."""
	supplier = _supplier()
	allowed = capabilities("Supplier")
	counters = {
		"orders_awaiting_ack": (
			"orders",
			lambda: frappe.db.count(
				"Purchase Order", {"supplier": supplier, "docstatus": 1, "automotive_ack_status": "Awaiting"}
			),
		),
		"open_orders": (
			"orders",
			lambda: frappe.db.count(
				"Purchase Order",
				{"supplier": supplier, "docstatus": 1, "status": ("not in", ("Completed", "Closed"))},
			),
		),
		"asns_in_transit": (
			"shipments",
			lambda: frappe.db.count(
				"Advance Shipping Notice", {"supplier": supplier, "status": ("in", ("In Transit", "Arrived"))}
			),
		),
		"open_discrepancies": (
			"receipts",
			lambda: frappe.db.count("Receipt Discrepancy", {"supplier": supplier, "status": "Open"}),
		),
		"unread_notices": (
			"notices",
			lambda: frappe.db.count("Supplier Notice", {"supplier": supplier, "read_at": ("is", "not set")}),
		),
		"schedules_to_acknowledge": (
			"schedules",
			lambda: frappe.db.count(
				"Supplier Delivery Schedule", {"supplier": supplier, "status": "Published"}
			),
		),
		"open_rfqs": ("quote", lambda: len(_rfqs(supplier, only_open=True))),
	}
	return {
		"supplier": supplier,
		"capabilities": allowed,
		**{key: count() for key, (capability, count) in counters.items() if capability in allowed},
	}


@frappe.whitelist()
def orders(status: str | None = None, page: int = 0) -> list[dict]:
	supplier = _supplier("orders")
	filters: dict[str, Any] = {"supplier": supplier, "docstatus": 1}
	if status == "awaiting":
		filters["automotive_ack_status"] = "Awaiting"
	return frappe.get_all(
		"Purchase Order",
		filters=filters,
		fields=[
			"name",
			"transaction_date",
			"schedule_date",
			"grand_total",
			"currency",
			"status",
			"automotive_ack_status",
			"automotive_release_type",
			"per_received",
		],
		order_by="transaction_date desc",
		limit_start=int(page) * PAGE,
		limit_page_length=PAGE,
		ignore_permissions=True,
	)


@frappe.whitelist()
def order(name: str) -> dict:
	supplier = _supplier("orders")
	po = _owned("Purchase Order", name, supplier)
	if po.docstatus != 1:
		raise frappe.PermissionError(_("Not found."))
	return {
		"name": po.name,
		"date": str(po.transaction_date),
		"currency": po.currency,
		"grand_total": po.grand_total,
		"status": po.status,
		"ack_status": po.automotive_ack_status,
		"lines": [
			{
				"row": i.name,
				"item_code": i.item_code,
				"item_name": i.item_name,
				"qty": i.qty,
				"uom": i.uom,
				"rate": i.rate,
				"schedule_date": str(i.schedule_date),
				"promised_date": str(i.automotive_promised_date or ""),
				"received_qty": i.received_qty,
			}
			for i in po.items
		],
		"acknowledgements": frappe.get_all(
			"Supplier Order Acknowledgement",
			filters={"purchase_order": po.name, "docstatus": 1},
			fields=["name", "ack_type", "acknowledged_at"],
			ignore_permissions=True,
		),
	}


@frappe.whitelist(methods=["POST"])
@idempotent("portal.supplier.acknowledge")
def acknowledge(
	purchase_order: str, response: str, lines: list | str | None = None, supplier_reference: str | None = None
) -> dict:
	supplier = _supplier("acknowledge")
	_owned("Purchase Order", purchase_order, supplier)
	rows = json_argument(lines, "lines", list) or []
	doc = frappe.get_doc(
		{
			"doctype": "Supplier Order Acknowledgement",
			"purchase_order": purchase_order,
			"ack_type": response,
			"supplier_reference": supplier_reference,
			"lines": [
				{
					"po_item": r["po_item"],
					"confirmed_qty": flt(r["confirmed_qty"]),
					"promised_date": r["promised_date"],
				}
				for r in rows
			],
		}
	)
	doc.insert()
	doc.submit()
	return {"acknowledgement": doc.name, "ack_type": doc.ack_type}


@frappe.whitelist(methods=["POST"])
@idempotent("portal.supplier.create_asn")
def create_asn(
	supplier_asn_no: str,
	ship_date: str,
	expected_arrival: str,
	lines: list | str,
	carrier: str | None = None,
	tracking_no: str | None = None,
	packages: int | None = None,
) -> dict:
	supplier = _supplier("ship")
	rows = json_argument(lines, "lines", list)
	for row in rows:
		_owned("Purchase Order", row["purchase_order"], supplier)
	doc = frappe.get_doc(
		{
			"doctype": "Advance Shipping Notice",
			"supplier": supplier,
			"supplier_asn_no": supplier_asn_no,
			"ship_date": ship_date,
			"expected_arrival": expected_arrival,
			"carrier": carrier,
			"tracking_no": tracking_no,
			"packages": packages,
			"items": [
				{
					"purchase_order": r["purchase_order"],
					"po_item": r["po_item"],
					"item_code": r["item_code"],
					"qty": flt(r["qty"]),
					"supplier_lot": r.get("supplier_lot"),
					"serial_nos": "\n".join(r.get("serial_nos") or []),
				}
				for r in rows
			],
		}
	)
	doc.insert()
	doc.submit()
	return {"asn": doc.name, "status": doc.status}


@frappe.whitelist()
def open_lines() -> list[dict]:
	"""Order lines the supplier can still announce on an ASN (ordered minus already announced)."""
	supplier = _supplier("shipments")
	rows = frappe.db.sql(
		"""select poi.parent purchase_order, poi.name po_item, poi.item_code, poi.item_name, poi.qty,
			poi.uom, poi.schedule_date, ifnull(poi.automotive_promised_date, poi.schedule_date) promised_date,
			i.has_batch_no, i.has_serial_no,
			ifnull((select sum(ai.qty) from `tabASN Item` ai join `tabAdvance Shipping Notice` a on a.name = ai.parent
				where a.docstatus = 1 and ai.po_item = poi.name), 0) announced
		from `tabPurchase Order Item` poi join `tabPurchase Order` po on po.name = poi.parent
		join `tabItem` i on i.name = poi.item_code
		where po.supplier = %s and po.docstatus = 1 and po.status not in ('Closed', 'Completed', 'On Hold')
		order by promised_date, po.name, poi.idx limit 500""",
		supplier,
		as_dict=True,
	)
	out = []
	for row in rows:
		row["open_qty"] = flt(row.qty) - flt(row.announced)
		if row["open_qty"] > 0:
			out.append(row)
	return out


@frappe.whitelist()
def asn(name: str) -> dict:
	doc = _owned("Advance Shipping Notice", name, _supplier("shipments"))
	return {
		"name": doc.name,
		"supplier_asn_no": doc.supplier_asn_no,
		"status": doc.status,
		"ship_date": str(doc.ship_date),
		"expected_arrival": str(doc.expected_arrival),
		"carrier": doc.carrier,
		"tracking_no": doc.tracking_no,
		"packages": doc.packages,
		"lines": [
			{
				"purchase_order": i.purchase_order,
				"item_code": i.item_code,
				"qty": i.qty,
				"received_qty": i.received_qty,
				"supplier_lot": i.supplier_lot,
			}
			for i in doc.items
		],
		"documents": shipment_documents(name),
	}


@frappe.whitelist()
def asns(page: int = 0) -> list[dict]:
	return frappe.get_all(
		"Advance Shipping Notice",
		filters={"supplier": _supplier("shipments"), "docstatus": ("<", 2)},
		fields=[
			"name",
			"supplier_asn_no",
			"ship_date",
			"expected_arrival",
			"status",
			"carrier",
			"tracking_no",
		],
		order_by="ship_date desc",
		limit_start=int(page) * PAGE,
		limit_page_length=PAGE,
		ignore_permissions=True,
	)


@frappe.whitelist()
def schedules() -> list[dict]:
	return frappe.get_all(
		"Supplier Delivery Schedule",
		filters={"supplier": _supplier("schedules"), "docstatus": 1},
		fields=[
			"name",
			"plant",
			"schedule_version",
			"horizon_start",
			"horizon_end",
			"status",
			"published_at",
		],
		order_by="published_at desc",
		limit_page_length=PAGE,
		ignore_permissions=True,
	)


@frappe.whitelist()
def schedule(name: str) -> dict:
	doc = _owned("Supplier Delivery Schedule", name, _supplier("schedules"))
	return {
		"name": doc.name,
		"version": doc.schedule_version,
		"status": doc.status,
		"plant": doc.plant,
		"lines": [
			{
				"item_code": ln.item_code,
				"delivery_date": str(ln.delivery_date),
				"qty": ln.qty,
				"firm": ln.firm,
				"purchase_order": ln.purchase_order,
			}
			for ln in doc.lines
		],
	}


@frappe.whitelist(methods=["POST"])
def acknowledge_schedule(name: str) -> str:
	from automotive_supply_chain.supplier_collaboration.schedules import acknowledge_schedule as ack

	_owned("Supplier Delivery Schedule", name, _supplier("confirm_schedule"))
	return ack(name)


@frappe.whitelist()
def discrepancies() -> list[dict]:
	return frappe.get_all(
		"Receipt Discrepancy",
		filters={"supplier": _supplier("receipts")},
		fields=[
			"name",
			"item_code",
			"discrepancy_type",
			"expected_qty",
			"received_qty",
			"rejected_qty",
			"status",
			"resolution",
			"creation",
		],
		order_by="creation desc",
		limit_page_length=PAGE,
		ignore_permissions=True,
	)


@frappe.whitelist()
def notices() -> list[dict]:
	return frappe.get_all(
		"Supplier Notice",
		filters={"supplier": _supplier("notices")},
		fields=["name", "subject", "notice_type", "message", "read_at", "creation"],
		order_by="creation desc",
		limit_page_length=PAGE,
		ignore_permissions=True,
	)


@frappe.whitelist(methods=["POST"])
def mark_notice_read(name: str) -> None:
	doc = _owned("Supplier Notice", name, _supplier("notices"))
	if not doc.read_at:
		frappe.db.set_value("Supplier Notice", name, "read_at", now_datetime())


@frappe.whitelist()
def remittances(page: int = 0) -> list[dict]:
	"""Payments made to the supplier and the invoices they settled (remittance advice)."""
	supplier = _supplier("payments")
	payments = frappe.get_all(
		"Payment Entry",
		filters={"party_type": "Supplier", "party": supplier, "docstatus": 1, "payment_type": "Pay"},
		fields=[
			"name",
			"posting_date",
			"paid_amount",
			"paid_from_account_currency",
			"reference_no",
			"reference_date",
		],
		order_by="posting_date desc",
		limit_start=int(page) * PAGE,
		limit_page_length=PAGE,
		ignore_permissions=True,
	)
	for pe in payments:
		pe["invoices"] = frappe.get_all(
			"Payment Entry Reference",
			filters={"parent": pe["name"]},
			fields=["reference_name", "allocated_amount"],
			ignore_permissions=True,
		)
		for ref in pe["invoices"]:
			ref["supplier_invoice"] = frappe.db.get_value(
				"Purchase Invoice", ref["reference_name"], "bill_no"
			)
	return payments


@frappe.whitelist()
def invoices_status() -> list[dict]:
	return frappe.get_all(
		"Purchase Invoice",
		filters={"supplier": _supplier("payments"), "docstatus": ("<", 2)},
		fields=[
			"name",
			"bill_no",
			"bill_date",
			"grand_total",
			"outstanding_amount",
			"status",
			"automotive_match_status",
			"due_date",
		],
		order_by="bill_date desc",
		limit_page_length=PAGE,
		ignore_permissions=True,
	)


def _rfqs(supplier: str, only_open: bool = False) -> list[dict]:
	rows = frappe.get_all(
		"Request for Quotation Supplier",
		filters={"supplier": supplier},
		fields=["parent", "quote_status"],
		ignore_permissions=True,
	)
	if not rows:
		return []
	status = {r.parent: r.quote_status for r in rows}
	filters: dict[str, Any] = {"name": ("in", list(status)), "docstatus": 1}
	rfqs_ = frappe.get_all(
		"Request for Quotation",
		filters=filters,
		fields=["name", "transaction_date", "schedule_date", "status", "company"],
		order_by="transaction_date desc",
		ignore_permissions=True,
	)
	for rfq in rfqs_:
		rfq["quote_status"] = status.get(rfq.name) or "Pending"
	if only_open:
		rfqs_ = [r for r in rfqs_ if r["quote_status"] == "Pending"]
	return rfqs_


@frappe.whitelist()
def rfqs() -> list[dict]:
	return _rfqs(_supplier("quote"))


def _invited(rfq_name: str, supplier: str):
	from erpnext.buying.doctype.request_for_quotation.request_for_quotation import get_quotable_rfq

	try:
		return get_quotable_rfq(rfq_name, supplier)
	except frappe.PermissionError:
		raise frappe.PermissionError(_("Not found.")) from None


@frappe.whitelist()
def rfq(name: str) -> dict:
	supplier = _supplier("quote")
	doc = _invited(name, supplier)
	quotes = frappe.get_all(
		"Supplier Quotation Item",
		filters={"request_for_quotation": doc.name, "docstatus": ("<", 2)},
		fields=["parent"],
		distinct=True,
		pluck="parent",
		ignore_permissions=True,
	)
	quotes = frappe.get_all(
		"Supplier Quotation",
		filters={"name": ("in", quotes or [""]), "supplier": supplier},
		fields=["name", "transaction_date", "valid_till", "grand_total", "currency", "status"],
		ignore_permissions=True,
	)
	return {
		"name": doc.name,
		"date": str(doc.transaction_date),
		"required_by": str(doc.schedule_date or ""),
		"message": doc.message_for_supplier,
		"terms": doc.terms,
		"company": doc.company,
		"items": [
			{
				"row": i.name,
				"item_code": i.item_code,
				"item_name": i.item_name,
				"description": i.description,
				"qty": i.qty,
				"uom": i.uom,
				"schedule_date": str(i.schedule_date or ""),
			}
			for i in doc.items
		],
		"quotations": quotes,
	}


@frappe.whitelist(methods=["POST"])
@idempotent("portal.supplier.submit_quotation")
def submit_quotation(
	rfq_name: str,
	lines: list | str,
	valid_till: str,
	lead_time_days: int | None = None,
	remarks: str | None = None,
) -> dict:
	"""The supplier's binding offer: a submitted Supplier Quotation (buyers cannot alter it)."""
	from erpnext.accounts.party import get_party_account_currency
	from erpnext.buying.doctype.request_for_quotation.request_for_quotation import (
		add_items,
		validate_existing_supplier_quotation,
	)

	supplier = _supplier("quote")
	doc = _invited(rfq_name, supplier)
	validate_existing_supplier_quotation(supplier, doc.name)
	rows = json_argument(lines, "lines", list)
	if not rows:
		frappe.throw(_("Quote at least one line."))
	for row in rows:
		if flt(row.get("rate")) <= 0 or flt(row.get("qty")) <= 0:
			frappe.throw(_("Every quoted line needs a positive quantity and price."))
	# ERPNext validates the supplier's payable account while building the quotation, which a
	# portal user may not read: the document is created by the system for the supplier user
	# (who stays its owner) after the checks above.
	with elevation.on_behalf_of(
		_("Supplier quotation for {0}").format(doc.name), doc.doctype, doc.name
	) as requester:
		sq = frappe.get_doc(
			{
				"doctype": "Supplier Quotation",
				"supplier": supplier,
				"company": doc.company,
				"transaction_date": frappe.utils.nowdate(),
				"valid_till": valid_till,
				"terms": remarks,
				"currency": get_party_account_currency("Supplier", supplier, doc.company),
				"buying_price_list": frappe.db.get_single_value("Buying Settings", "buying_price_list"),
			}
		)
		add_items(sq, supplier, doc, [{"name": r["row"], "qty": r["qty"], "rate": r["rate"]} for r in rows])
		if lead_time_days is not None:
			for item in sq.items:
				item.lead_time_days = int(lead_time_days)
		sq.run_method("set_missing_values")
		sq.insert()
		sq.submit()
		sq.db_set("owner", requester, update_modified=False)  # the supplier user's offer
		frappe.db.set_value(
			"Request for Quotation Supplier",
			{"parent": doc.name, "supplier": supplier},
			"quote_status",
			"Received",
		)
	return {"supplier_quotation": sq.name, "grand_total": sq.grand_total, "currency": sq.currency}


@frappe.whitelist()
def quotations() -> list[dict]:
	return frappe.get_all(
		"Supplier Quotation",
		filters={"supplier": _supplier("quote"), "docstatus": ("<", 2)},
		fields=["name", "transaction_date", "valid_till", "grand_total", "currency", "status"],
		order_by="transaction_date desc",
		limit_page_length=PAGE,
		ignore_permissions=True,
	)


@frappe.whitelist()
def receipts(page: int = 0) -> list[dict]:
	"""Goods receipts against the supplier's deliveries (accepted / rejected quantities)."""
	supplier = _supplier("receipts")
	rows = frappe.get_all(
		"Purchase Receipt",
		filters={"supplier": supplier, "docstatus": 1, "is_return": 0},
		fields=["name", "posting_date", "supplier_delivery_note", "automotive_asn", "status"],
		order_by="posting_date desc",
		limit_start=int(page) * PAGE,
		limit_page_length=PAGE,
		ignore_permissions=True,
	)
	for row in rows:
		row["lines"] = frappe.get_all(
			"Purchase Receipt Item",
			filters={"parent": row["name"]},
			fields=["item_code", "received_qty", "qty", "rejected_qty", "purchase_order"],
			ignore_permissions=True,
		)
	return rows


@frappe.whitelist()
def shipment_documents(asn: str) -> list[dict]:
	"""Files attached to one of the supplier's ASNs (packing lists, certificates)."""
	_owned("Advance Shipping Notice", asn, _supplier("shipments"))
	return frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Advance Shipping Notice", "attached_to_name": asn},
		fields=["name", "file_name", "file_url", "file_size", "creation"],
		order_by="creation asc",
		ignore_permissions=True,
	)
