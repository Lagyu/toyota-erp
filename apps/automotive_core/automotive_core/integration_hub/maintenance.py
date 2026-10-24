"""Daily pruning of short-lived integration records."""

from __future__ import annotations

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from automotive_core.utils import commit


def prune() -> None:
	tolerance = cint(frappe.db.get_single_value("Automotive Settings", "webhook_tolerance_seconds")) or 300
	frappe.db.delete(
		"Integration Nonce", {"seen_at": ("<", add_to_date(now_datetime(), seconds=-2 * tolerance))}
	)
	frappe.db.delete("Idempotency Record", {"expires_at": ("<", now_datetime())})
	commit()
