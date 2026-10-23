"""Inbound integration handler for plant MES messages (message_type production_event)."""

from __future__ import annotations

from typing import Literal

import frappe
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
	if event.event == "unit_built":
		se = build_entry(event.work_order, 1, [event.serial_no] if event.serial_no else None,
			[c.model_dump() for c in event.components])
	else:
		se = build_entry(event.work_order, event.qty)
	return "Stock Entry", se.name
