"""Supply-chain work queues for automotive_core.ops.health.metrics."""

from __future__ import annotations

import frappe


def metrics() -> list[tuple[str, str, dict, float]]:
	return [
		("erp_match_exceptions_open", "Invoice match exceptions awaiting a decision", {},
			float(frappe.db.count("Invoice Match Exception", {"status": "Open"}))),
		("erp_receipt_discrepancies_open", "Open receipt discrepancies", {},
			float(frappe.db.count("Receipt Discrepancy", {"status": "Open"}))),
		("erp_transport_exceptions_open", "Open transport exceptions", {},
			float(frappe.db.count("Transport Exception", {"status": "Open"}))),
		("erp_dealer_orders_to_accept", "Dealer orders awaiting acceptance", {},
			float(frappe.db.count("Dealer Order", {"status": "Submitted"}))),
	]
