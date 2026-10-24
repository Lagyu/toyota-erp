"""Shop-floor and quality work queues for automotive_core.ops.health.metrics."""

from __future__ import annotations

import frappe


def metrics() -> list[tuple[str, str, dict, float]]:
	return [
		("erp_quality_holds_active", "Active quality holds", {},
			float(frappe.db.count("Quality Hold", {"status": "Active"}))),
		("erp_andon_open", "Andon events not yet restarted", {},
			float(frappe.db.count("Andon Event", {"status": ("in", ("Raised", "Acknowledged", "Contained"))}))),
		("erp_kanban_signals_overdue", "Kanban signals escalated as overdue", {},
			float(frappe.db.count("Kanban Signal", {"status": "Escalated"}))),
		("erp_planning_runs_failed", "Planning runs that failed", {},
			float(frappe.db.count("Planning Run", {"status": "Failed"}))),
	]
