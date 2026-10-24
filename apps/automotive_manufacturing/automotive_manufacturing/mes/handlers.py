"""Inbound integration handler for plant MES messages (message_type production_event)."""

from __future__ import annotations

from typing import Literal

import frappe
from frappe.utils import flt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from automotive_core.integration_hub.inbound import InboundRejected
from automotive_manufacturing.mes.production import build_entry


class ScannedComponent(BaseModel):
	model_config = ConfigDict(extra="forbid")
	item_code: str
	serial_no: str | None = None
	batch_no: str | None = None


class ProductionEvent(BaseModel):
	model_config = ConfigDict(extra="forbid")
	event: Literal["unit_built", "batch_built"]
	work_order: str
	qty: float = Field(default=1, gt=0)
	serial_no: str | None = None
	components: list[ScannedComponent] = []


def production_event(message, partner, data: dict):
	try:
		event = ProductionEvent.model_validate(data)
	except ValidationError as exc:
		raise InboundRejected(f"invalid production_event: {exc}") from exc
	if not frappe.db.exists("Work Order", event.work_order):
		raise InboundRejected(f"unknown work order {event.work_order}")
	# Runs as the partner's service user: its plant User Permissions limit which orders it may report.
	if not frappe.has_permission("Work Order", "read", doc=event.work_order):
		raise InboundRejected(f"partner {partner.name} may not report on work order {event.work_order}")
	# what no retry can fix is rejected now: a closed order, or more than it may still produce
	# (ERPNext's over-production allowance included); a shortage of components is retried
	order = frappe.db.get_value("Work Order", event.work_order, ["docstatus", "status", "qty", "produced_qty"],
		as_dict=True)
	if order.docstatus != 1 or order.status in ("Completed", "Closed", "Stopped", "Cancelled"):
		raise InboundRejected(f"work order {event.work_order} is {order.status}: it takes no more reports")
	allowance = flt(frappe.db.get_single_value("Manufacturing Settings", "overproduction_percentage_for_work_order"))
	open_qty = flt(order.qty) * (1 + allowance / 100) - flt(order.produced_qty)
	reported = 1 if event.event == "unit_built" else event.qty
	if reported > open_qty + 1e-9:
		raise InboundRejected(f"work order {event.work_order} can take {max(open_qty, 0):g} more, {reported:g} reported")
	if event.event == "unit_built":
		se = build_entry(event.work_order, 1, [event.serial_no] if event.serial_no else None,
			[c.model_dump() for c in event.components])
	else:
		se = build_entry(event.work_order, event.qty)
	return "Stock Entry", se.name
