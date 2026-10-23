"""Adapter interface for outbound delivery."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DeliveryResult:
	ok: bool
	status_code: int | None = None
	error: str | None = None
	response_excerpt: str | None = None


class Adapter:
	name = "base"

	def send(self, partner, message) -> DeliveryResult:  # pragma: no cover - interface
		raise NotImplementedError

	@staticmethod
	def body(partner, message) -> tuple[bytes, str]:
		"""Serialized body and content type for the partner's message format."""
		if partner.message_format == "Hinode EDI":
			import json

			from automotive_core.integration_hub.adapters.edi import encode

			return encode(json.loads(message.payload)).encode(), "application/edi-x12"
		return message.payload.encode(), "application/json"
