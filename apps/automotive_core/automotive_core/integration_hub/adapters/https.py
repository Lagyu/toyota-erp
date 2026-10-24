"""Signed HTTPS webhook delivery with an outbound host allowlist (ERP-11, §6.3)."""

from __future__ import annotations

from urllib.parse import urlparse

import frappe
import requests

from automotive_core.integration_hub import signing
from automotive_core.integration_hub.adapters.base import Adapter, DeliveryResult

TIMEOUT_SECONDS = 10


def allowed_hosts() -> set[str]:
	text = frappe.get_cached_doc("Automotive Settings").outbound_host_allowlist or ""
	return {line.strip().lower() for line in text.splitlines() if line.strip()}


def check_destination(url: str | None) -> str | None:
	"""Return an error string when the URL may not be called, else None."""
	if not url:
		return "no endpoint configured"
	parsed = urlparse(url)
	if parsed.scheme != "https":
		return "only https endpoints are allowed"
	if (parsed.hostname or "").lower() not in allowed_hosts():
		return f"host {parsed.hostname} is not in the outbound allowlist"
	return None


class HttpsWebhookAdapter(Adapter):
	name = "HTTPS Webhook"

	def send(self, partner, message) -> DeliveryResult:
		problem = check_destination(partner.endpoint_url)
		if problem:
			return DeliveryResult(ok=False, status_code=0, error=problem)
		secret = partner.get_password("outbound_secret", raise_exception=False)
		if not secret:
			return DeliveryResult(ok=False, status_code=0, error="no outbound signing secret configured")
		body, content_type = self.body(partner, message)
		headers = {
			"Content-Type": content_type,
			"User-Agent": "toyota-erp-clone-outbox/1",
			"X-Hinode-Event": message.event_type,
			"X-Hinode-Delivery": message.name,
			"Idempotency-Key": message.idempotency_key,
			"X-Correlation-ID": message.correlation_id or "",
			signing.HEADER: signing.build_header(partner.key_id or partner.name, secret, body),
		}
		try:
			response = requests.post(
				partner.endpoint_url,
				data=body,
				headers=headers,
				timeout=TIMEOUT_SECONDS,
				allow_redirects=False,
			)
		except requests.RequestException as exc:
			return DeliveryResult(ok=False, status_code=0, error=f"{type(exc).__name__}: {exc}"[:500])
		ok = 200 <= response.status_code < 300
		return DeliveryResult(
			ok=ok,
			status_code=response.status_code,
			error=None if ok else f"HTTP {response.status_code}",
			response_excerpt=response.text[:500],
		)
