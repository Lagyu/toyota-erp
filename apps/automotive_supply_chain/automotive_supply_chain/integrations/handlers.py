"""Inbound partner messages (EDI/JSON) -> the same documents the portals create.

Handlers run as the partner's service user (a portal user of that supplier/dealer),
so party scoping and validations are identical to portal submissions. Each handler
also deduplicates on the partner's own business reference (ASN number, dealer
reference) in addition to the message id, so exactly-once holds even if a partner
resends the same business document under a new message id.
"""

from __future__ import annotations

from datetime import date, datetime

import frappe
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
		"docstatus": 1}, ["name", "items"], as_dict=True)
	if not order:
		raise InboundRejected(f"no accepted order {msg.dealer_reference}")
	values = {"doctype": "Dealer Order Change", "dealer_order": order.name, "change_type": msg.change_type,
		"item_row": msg.item_row, "new_qty": msg.new_qty, "new_delivery_date": msg.new_delivery_date,
		"reason": msg.reason}
	if msg.new_selections:
		model = frappe.get_all("Dealer Order Item", filters={"parent": order.name, "idx": msg.item_row},
			pluck="vehicle_model")
		values["new_configuration"] = configure(model[0], msg.new_selections)["configuration"]
	doc = frappe.get_doc(values)
	doc.insert()
	doc.submit()
	return doc.doctype, doc.name
