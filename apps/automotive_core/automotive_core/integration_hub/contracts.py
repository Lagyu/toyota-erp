"""Schema-validated message envelopes shared by every adapter (ERP-11).

Message *bodies* are validated by the handler that owns the message type (each app
declares a pydantic model next to its handler); the envelope is common.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

INBOUND_MESSAGE_TYPES = (
	"dealer_order",
	"dealer_order_change",
	"asn",
	"po_acknowledgement",
	"production_event",
	"inventory_adjustment",
	"supplier_invoice",
	"shipment_status",
	"bank_statement",
)

MessageType = Literal[
	"dealer_order",
	"dealer_order_change",
	"asn",
	"po_acknowledgement",
	"production_event",
	"inventory_adjustment",
	"supplier_invoice",
	"shipment_status",
	"bank_statement",
]


class InboundEnvelope(BaseModel):
	model_config = ConfigDict(extra="forbid")

	message_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
	message_type: MessageType
	schema_version: Literal["v1"] = "v1"
	sent_at: dt.datetime | None = None
	data: dict[str, Any]


class OutboundEnvelope(BaseModel):
	model_config = ConfigDict(extra="forbid")

	event_id: str
	event_type: str
	schema_version: Literal["v1"] = "v1"
	occurred_at: dt.datetime
	correlation_id: str | None = None
	data: dict[str, Any]


class AcceptedResponse(BaseModel):
	status: Literal["accepted", "duplicate"]
	inbound_id: str
	processing_status: str
	result_doctype: str | None = None
	result_name: str | None = None
