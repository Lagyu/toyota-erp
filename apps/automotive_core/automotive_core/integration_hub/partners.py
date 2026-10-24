"""Integration Partner status and local exchange scheduling."""

from __future__ import annotations

import secrets

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from automotive_core.governance import audit
from automotive_core.integration_hub.adapters.https import check_destination

MAX_OVERLAP_HOURS = 168


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


@frappe.whitelist(methods=["POST"])
def rotate_inbound_secret(partner: str, overlap_hours: int = 24) -> dict:
	"""A new inbound secret for the partner. The current one stays valid for
	``overlap_hours``, so the partner can switch without refused deliveries. The new secret is
	returned once: hand it to the partner over a secure channel."""
	frappe.only_for(("IT Operator", "System Manager"))
	overlap = cint(overlap_hours)
	if not 0 <= overlap <= MAX_OVERLAP_HOURS:
		frappe.throw(_("The overlap must be between 0 and {0} hours.").format(MAX_OVERLAP_HOURS))
	doc = frappe.get_doc("Integration Partner", partner)
	current = doc.get_password("inbound_secret", raise_exception=False)
	secret = secrets.token_urlsafe(32)
	doc.previous_inbound_secret = current if current and overlap else None
	doc.previous_inbound_secret_until = add_to_date(now_datetime(), hours=overlap) if current and overlap else None
	doc.inbound_secret = secret
	doc.save()
	audit.record("integration.inbound_secret_rotated", doc.doctype, doc.name,
		{"overlap_hours": overlap, "previous_valid_until": str(doc.previous_inbound_secret_until or "")})
	return {"secret": secret, "previous_valid_until": doc.previous_inbound_secret_until}
