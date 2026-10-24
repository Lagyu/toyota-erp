"""Inbound partner messages (EDI/JSON) -> the same documents the portals create.

Handlers run as the partner's service user (a portal user of that supplier/dealer),
so party scoping and validations are identical to portal submissions. Each handler
also deduplicates on the partner's own business reference (ASN number, dealer
reference, invoice number) in addition to the message id, so exactly-once holds even if a
partner resends the same business document under a new message id. Carriers and the
suppliers' invoicing systems need no portal: their party is the Integration Partner's own,
and what they cause is posted as a recorded system step.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import frappe
from frappe import _
from frappe.utils import flt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from automotive_core.governance.elevation import on_behalf_of
from automotive_core.integration_hub.inbound import InboundRejected
from automotive_supply_chain.partners import party_of_user


class Strict(BaseModel):
	model_config = ConfigDict(extra="forbid")


class AsnLine(Strict):
	purchase_order: str
	po_item: str
	item_code: str
	qty: float = Field(gt=0)
	supplier_lot: str | None = None
	serial_nos: list[str] = []


class AsnMessage(Strict):
	supplier_asn_no: str
	ship_date: datetime
	expected_arrival: datetime
	carrier: str | None = None
	tracking_no: str | None = None
	packages: int | None = None
	lines: list[AsnLine] = Field(min_length=1)


class AckLine(Strict):
	po_item: str
	confirmed_qty: float = Field(ge=0)
	promised_date: date


class AckMessage(Strict):
	purchase_order: str
	response: str = Field(pattern="^(Accepted|Accepted With Changes|Rejected)$")
	supplier_reference: str | None = None
	lines: list[AckLine] = []


class DealerOrderLine(Strict):
	vehicle_model: str
	selections: dict[str, str]
	qty: int = Field(gt=0, default=1)


class DealerOrderMessage(Strict):
	dealer_reference: str
	order_type: str = Field(default="Customer Order", pattern="^(Customer Order|Stock Order|Demonstrator)$")
	requested_delivery_date: date
	company: str
	lines: list[DealerOrderLine] = Field(min_length=1)


class DealerOrderChangeMessage(Strict):
	dealer_reference: str
	change_type: str = Field(pattern="^(Change Quantity|Change Delivery Date|Change Configuration|Cancel Order)$")
	item_row: int = 1
	new_qty: int | None = None
	new_delivery_date: date | None = None
	new_selections: dict[str, str] | None = None
	reason: str


class ShipmentStatusMessage(Strict):
	manifest: str
	status: str = Field(pattern="^(in_transit|delivered|exception)$")
	at: datetime
	received_by: str | None = Field(default=None, max_length=140)
	exception_type: str = Field(default="Delay", pattern="^(Delay|Damage|Loss|Wrong Destination|Documentation)$")
	serial_no: str | None = Field(default=None, max_length=140)
	description: str | None = Field(default=None, max_length=2000)
	carrier_reference: str | None = Field(default=None, max_length=140)


class SupplierInvoiceLine(Strict):
	item_code: str
	qty: float = Field(gt=0)
	rate: float = Field(ge=0)


class SupplierInvoiceMessage(Strict):
	invoice_no: str = Field(min_length=1, max_length=140)
	invoice_date: date
	purchase_receipt: str
	lines: list[SupplierInvoiceLine] = Field(min_length=1)


class InventoryCountLine(Strict):
	item_code: str
	counted_qty: float = Field(ge=0)
	batch_no: str | None = None


class InventoryAdjustmentMessage(Strict):
	count_reference: str = Field(min_length=1, max_length=100)
	counted_at: datetime
	warehouse: str | None = None
	lines: list[InventoryCountLine] = Field(min_length=1)


def _parse(model, data):
	try:
		return model.model_validate(data)
	except ValidationError as exc:
		raise InboundRejected(f"invalid {model.__name__}: {exc}") from exc


def _party(expected: str) -> str:
	mine = party_of_user()
	if not mine or mine[0] != expected:
		raise InboundRejected(f"service user is not a {expected} portal user")
	return mine[1]


def _partner_party(partner, expected: str) -> str:
	"""The party set on the Integration Partner (IT's configuration); a service user that is a
	portal user must belong to the same one."""
	if partner.party_type != expected or not partner.party:
		raise InboundRejected(f"partner {partner.name} has no {expected} party")
	mine = party_of_user()
	if mine and mine != (expected, partner.party):
		raise InboundRejected(f"service user does not belong to {partner.party}")
	return partner.party


def _local(moment: datetime) -> datetime:
	"""A partner's timestamp in the system time zone, as Frappe stores datetimes."""
	from frappe.utils import convert_utc_to_system_timezone

	if moment.tzinfo is None:
		return moment
	utc = moment.astimezone(UTC).replace(tzinfo=None)
	return convert_utc_to_system_timezone(utc).replace(tzinfo=None)


def asn(message, partner, data):
	msg = _parse(AsnMessage, data)
	supplier = _party("Supplier")
	existing = frappe.db.get_value("Advance Shipping Notice", {"supplier": supplier, "supplier_asn_no":
		msg.supplier_asn_no, "docstatus": 1}, "name")
	if existing:
		return "Advance Shipping Notice", existing
	doc = frappe.get_doc({
		"doctype": "Advance Shipping Notice", "supplier": supplier, "supplier_asn_no": msg.supplier_asn_no,
		"ship_date": msg.ship_date, "expected_arrival": msg.expected_arrival, "carrier": msg.carrier,
		"tracking_no": msg.tracking_no, "packages": msg.packages,
		"items": [{**ln.model_dump(exclude={"serial_nos"}), "serial_nos": "\n".join(ln.serial_nos)} for ln in msg.lines],
	})
	doc.insert()
	doc.submit()
	return doc.doctype, doc.name


def po_acknowledgement(message, partner, data):
	msg = _parse(AckMessage, data)
	supplier = _party("Supplier")
	if frappe.db.get_value("Purchase Order", msg.purchase_order, "supplier") != supplier:
		raise InboundRejected("purchase order belongs to another supplier")
	doc = frappe.get_doc({"doctype": "Supplier Order Acknowledgement", "purchase_order": msg.purchase_order,
		"ack_type": msg.response, "supplier_reference": msg.supplier_reference,
		"lines": [ln.model_dump() for ln in msg.lines]})
	doc.insert()
	doc.submit()
	return doc.doctype, doc.name


def dealer_order(message, partner, data):
	from automotive_manufacturing.product_engineering.configuration import configure

	msg = _parse(DealerOrderMessage, data)
	dealer = _party("Customer")
	existing = frappe.db.get_value("Dealer Order", {"dealer": dealer, "dealer_reference": msg.dealer_reference,
		"docstatus": ("<", 2)}, "name")
	if existing:
		return "Dealer Order", existing
	items = []
	for line in msg.lines:
		cfg = configure(line.vehicle_model, line.selections)
		items.append({"vehicle_model": line.vehicle_model, "configuration": cfg["configuration"], "qty": line.qty})
	doc = frappe.get_doc({"doctype": "Dealer Order", "dealer": dealer, "dealer_reference": msg.dealer_reference,
		"company": msg.company, "order_type": msg.order_type, "requested_delivery_date": msg.requested_delivery_date,
		"items": items})
	doc.insert()
	doc.submit()
	return doc.doctype, doc.name


def dealer_order_change(message, partner, data):
	from automotive_manufacturing.product_engineering.configuration import configure

	msg = _parse(DealerOrderChangeMessage, data)
	dealer = _party("Customer")
	order = frappe.db.get_value("Dealer Order", {"dealer": dealer, "dealer_reference": msg.dealer_reference,
		"docstatus": 1}, "name")
	if not order:
		raise InboundRejected(f"no accepted order {msg.dealer_reference}")
	values = {"doctype": "Dealer Order Change", "dealer_order": order, "change_type": msg.change_type,
		"item_row": msg.item_row, "new_qty": msg.new_qty, "new_delivery_date": msg.new_delivery_date,
		"reason": msg.reason}
	if msg.new_selections:
		model = frappe.get_all("Dealer Order Item", filters={"parent": order, "idx": msg.item_row},
			pluck="vehicle_model")
		values["new_configuration"] = configure(model[0], msg.new_selections)["configuration"]
	doc = frappe.get_doc(values)
	doc.insert()
	doc.submit()
	return doc.doctype, doc.name


def inventory_adjustment(message, partner, data):
	"""A supplier's count of the consignment (VMI) stock it owns at our plant. It becomes a
	Cycle Count for review — expected and counted quantity per item and batch — never a direct
	stock change: a warehouse clerk compares and posts it. Created as a recorded system step
	(the partner has no desk rights); the partner's count reference makes resends idempotent."""
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	from automotive_core.governance.elevation import on_behalf_of

	msg = _parse(InventoryAdjustmentMessage, data)
	supplier = _party("Supplier")
	reference = f"{partner.name}:{msg.count_reference}"
	existing = frappe.db.get_value("Cycle Count", {"partner_reference": reference, "docstatus": ("<", 2)}, "name")
	if existing:
		return "Cycle Count", existing
	agreements = {a.consignment_warehouse: a for a in frappe.get_all("Consignment Agreement",
		filters={"supplier": supplier, "docstatus": 1, "status": "Active"},
		fields=["name", "company", "consignment_warehouse"])}
	agreement = agreements.get(msg.warehouse) if msg.warehouse else (
		next(iter(agreements.values())) if len(agreements) == 1 else None)
	if not agreement:
		raise InboundRejected(f"no active consignment agreement of {supplier} for warehouse {msg.warehouse or '(none given)'}")
	warehouse = agreement.consignment_warehouse
	covered = set(frappe.get_all("Consignment Agreement Item", filters={"parent": agreement.name}, pluck="item_code"))
	for line in msg.lines:
		if line.item_code not in covered:
			raise InboundRejected(f"{line.item_code} is not consigned under {agreement.name}")
		if frappe.get_cached_value("Item", line.item_code, "has_serial_no"):
			raise InboundRejected(f"{line.item_code} is serialised: count it by receipt, not by quantity")
		if frappe.get_cached_value("Item", line.item_code, "has_batch_no") and not line.batch_no:
			raise InboundRejected(f"{line.item_code} is counted by batch: give batch_no")
	with on_behalf_of(_("Inventory report {0} from {1}").format(msg.count_reference, supplier),
			"Integration Inbound Message", message.name):
		# our own records of the supplier's stock: read with system rights, not the partner's
		lines = [{"item_code": line.item_code, "batch_no": line.batch_no, "counted_qty": line.counted_qty,
			"expected_qty": flt(get_batch_qty(batch_no=line.batch_no, warehouse=warehouse)) if line.batch_no
				else flt(frappe.db.get_value("Bin", {"item_code": line.item_code, "warehouse": warehouse}, "actual_qty"))}
			for line in msg.lines]
		doc = frappe.get_doc({"doctype": "Cycle Count", "company": agreement.company, "warehouse": warehouse,
			"count_date": msg.counted_at.date(), "count_class": "Full", "source": "Partner Report",
			"supplier": supplier, "partner_reference": reference, "lines": lines})
		doc.insert()
	return doc.doctype, doc.name


def shipment_status(message, partner, data):
	"""A carrier's report on a shipment it carries (ERP-06.4, ERP-11.4). ``delivered`` is the
	proof of delivery the warehouse would otherwise record; ``exception`` (delay, damage, loss,
	wrong destination, papers) opens a Transport Exception for logistics; ``in_transit``
	confirms the dispatch and may carry the carrier's own reference. A delivery reported twice
	is answered with the manifest again."""
	from automotive_supply_chain.logistics.operations import deliver

	msg = _parse(ShipmentStatusMessage, data)
	carrier = _partner_party(partner, "Supplier")
	manifest = frappe.db.get_value("Shipment Manifest", msg.manifest, ["name", "carrier", "docstatus",
		"delivery_status", "transport_booking", "company"], as_dict=True)
	if not manifest or manifest.carrier != carrier or manifest.docstatus != 1:
		raise InboundRejected(f"no dispatched manifest {msg.manifest} carried by {carrier}")
	at = _local(msg.at)
	with on_behalf_of(_("Shipment status from carrier {0}").format(carrier), "Shipment Manifest", manifest.name):
		if msg.status == "exception":
			exception = frappe.get_doc({"doctype": "Transport Exception", "shipment_manifest": manifest.name,
				"transport_booking": manifest.transport_booking, "carrier": carrier, "exception_type": msg.exception_type,
				"reported_at": at, "serial_no": msg.serial_no, "status": "Open",
				"description": msg.description or msg.exception_type})
			exception.insert(ignore_permissions=True)
			return exception.doctype, exception.name
		if manifest.delivery_status in ("Delivered", "Partially Delivered", "Exception"):
			return "Shipment Manifest", manifest.name  # reported already
		if msg.status == "delivered":
			deliver(manifest.name, msg.received_by or carrier, at)
		elif msg.carrier_reference and manifest.transport_booking:
			frappe.db.set_value("Transport Booking", manifest.transport_booking, "carrier_reference",
				msg.carrier_reference)
	return "Shipment Manifest", manifest.name


def supplier_invoice(message, partner, data):
	"""A supplier's invoice for one of its receipts (ERP-05, ERP-09.2): a draft Purchase Invoice
	made from the receipt with the supplier's quantities and prices. The three-way match checks
	it against the order and the receipt as for one typed in, and the AP clerk reviews and
	submits it. Resent with the same invoice number, it is answered with the same draft."""
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

	msg = _parse(SupplierInvoiceMessage, data)
	supplier = _partner_party(partner, "Supplier")
	existing = frappe.db.get_value("Purchase Invoice", {"supplier": supplier, "bill_no": msg.invoice_no,
		"docstatus": ("<", 2)}, "name")
	if existing:
		return "Purchase Invoice", existing
	receipt = frappe.db.get_value("Purchase Receipt", msg.purchase_receipt, ["supplier", "docstatus", "is_return"],
		as_dict=True)
	if not receipt or receipt.supplier != supplier or receipt.docstatus != 1 or receipt.is_return:
		raise InboundRejected(f"no receipt {msg.purchase_receipt} of {supplier} to invoice")
	lines = {}
	for line in msg.lines:
		if line.item_code in lines:
			raise InboundRejected(f"item {line.item_code} is invoiced twice")
		lines[line.item_code] = line
	with on_behalf_of(_("Invoice {0} from supplier {1}").format(msg.invoice_no, supplier), "Purchase Receipt",
			msg.purchase_receipt):
		invoice = frappe.get_doc(make_purchase_invoice(msg.purchase_receipt))
		unknown = sorted(set(lines) - {row.item_code for row in invoice.items})
		if unknown:
			raise InboundRejected(f"not on receipt {msg.purchase_receipt} (or invoiced already): {', '.join(unknown)}")
		rows: dict[str, list] = {}
		for row in invoice.items:
			if row.item_code in lines:
				rows.setdefault(row.item_code, []).append(row)
		# an item received on several lines (put-away into forward and reserve storage, say):
		# the invoiced quantity fills them in order, any excess on the last for the match to see
		for item_code, item_rows in rows.items():
			left = lines[item_code].qty
			for n, row in enumerate(item_rows):
				share = left if n == len(item_rows) - 1 else min(left, flt(row.qty))
				row.qty, row.rate = share, lines[item_code].rate
				left -= share
		invoice.set("items", [row for row in invoice.items if row.item_code in lines and flt(row.qty) > 0])
		invoice.bill_no, invoice.bill_date = msg.invoice_no, msg.invoice_date
		invoice.insert()
	return invoice.doctype, invoice.name

