"""Partner adapters: HTTPS webhook (live) and local file drop (local, durable)."""

from __future__ import annotations

from automotive_core.integration_hub.adapters.base import Adapter


def get_adapter(partner) -> Adapter:
	from automotive_core.integration_hub.adapters.file_drop import FileDropAdapter
	from automotive_core.integration_hub.adapters.https import HttpsWebhookAdapter

	adapters = {
		"HTTPS Webhook": HttpsWebhookAdapter,
		"Local File Drop": FileDropAdapter,
	}
	try:
		return adapters[partner.adapter]()
	except KeyError:
		raise ValueError(f"unknown adapter {partner.adapter!r}") from None
