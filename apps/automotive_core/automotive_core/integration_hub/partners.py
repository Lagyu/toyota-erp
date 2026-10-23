"""Integration Partner status and local exchange scheduling."""

from __future__ import annotations

import frappe

from automotive_core.integration_hub.adapters.https import check_destination


def connection_status(partner) -> str:
	"""Never reports Live unless a real HTTPS endpoint with credentials has delivered."""
	if partner.adapter == "Local File Drop":
		return "Local Adapter Only"
	has_secret = bool(partner.get_password("outbound_secret", raise_exception=False))
	if check_destination(partner.endpoint_url) or not has_secret:
		return "Not Configured"
	if partner.last_success_at and (not partner.last_failure_at or partner.last_success_at >= partner.last_failure_at):
		return "Live"
	return "Configured (Unverified)"


def poll_file_drops() -> None:
	from automotive_core.integration_hub.adapters.file_drop import poll_inbox

	for name in frappe.get_all("Integration Partner", filters={"is_active": 1, "adapter": "Local File Drop"}, pluck="name"):
		poll_inbox(name)
