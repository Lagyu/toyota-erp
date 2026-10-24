"""Shift handover: results computed from transactions at hand-over time (ERP-04)."""

from __future__ import annotations

import datetime as dt

import frappe
from frappe import _
from frappe.utils import flt, get_datetime, get_time, getdate, now_datetime

from automotive_core.access.plant_scope import get_scoped_doc


def shift_window(line: str, shift_date, shift_code: str) -> tuple[dt.datetime, dt.datetime]:
	pattern = frappe.get_cached_value("Production Line", line, "shift_pattern")
	slot = next((s for s in frappe.get_cached_doc("Shift Pattern", pattern).shifts if s.shift_code == shift_code), None)
	if not slot:
		frappe.throw(_("Shift {0} is not in pattern {1}.").format(shift_code, pattern))
	day = getdate(shift_date)
	start = dt.datetime.combine(day, get_time(slot.start_time))
	end = dt.datetime.combine(day, get_time(slot.end_time))
	if end <= start:
		end += dt.timedelta(days=1)
	return start, end


def compute(doc) -> None:
	start, end = shift_window(doc.production_line, doc.shift_date, doc.shift_code)
	schedule = frappe.db.get_value("Plant Schedule", {"production_line": doc.production_line,
		"schedule_date": doc.shift_date}, "name")
	doc.planned_units = frappe.db.count("Plant Schedule Slot", {"parent": schedule, "shift_code": doc.shift_code}) if schedule else 0
	doc.built_units = frappe.db.count("Vehicle Unit", {"production_line": doc.production_line,
		"built_at": ("between", (start, end))})
	if not doc.built_units:
		doc.built_units = int(flt(frappe.db.sql(
			"""select sum(se.fg_completed_qty) from `tabStock Entry` se join `tabWork Order` wo on wo.name = se.work_order
			where se.docstatus=1 and se.purpose='Manufacture' and wo.automotive_production_line=%s
			and timestamp(se.posting_date, se.posting_time) between %s and %s""",
			(doc.production_line, start, end))[0][0]))
	doc.scrap_qty = flt(frappe.db.sql(
		"""select sum(jc.process_loss_qty) from `tabJob Card` jc join `tabWork Order` wo on wo.name = jc.work_order
		where jc.docstatus=1 and wo.automotive_production_line=%s and jc.modified between %s and %s""",
		(doc.production_line, start, end))[0][0])
	events = frappe.get_all("Andon Event", filters={"production_line": doc.production_line,
		"raised_at": ("between", (start, end))}, fields=["name", "status", "duration_minutes", "stop_type"])
	doc.andon_events = len(events)
	doc.downtime_minutes = sum(flt(e.duration_minutes) for e in events if e.stop_type == "Line Stop")
	open_andons = [e.name for e in events if e.status not in ("Closed",)]
	holds = frappe.get_all("Quality Hold", filters={"production_line": doc.production_line, "status": "Active"},
		pluck="name")
	holds += frappe.db.sql_list(
		"""select qh.name from `tabQuality Hold` qh join `tabWork Order` wo on wo.name = qh.work_order
		where qh.status='Active' and wo.automotive_production_line=%s""", doc.production_line)
	items = []
	if open_andons:
		items.append(_("Open andons: {0}").format(", ".join(open_andons)))
	if holds:
		items.append(_("Active holds: {0}").format(", ".join(sorted(set(holds)))))
	doc.open_items = "\n".join(items) or None


@frappe.whitelist(methods=["POST"])
def acknowledge(name: str) -> str:
	doc = get_scoped_doc("Shift Handover", name)
	if frappe.session.user != doc.incoming_supervisor:
		raise frappe.PermissionError(_("Only the incoming supervisor can acknowledge this handover."))
	if doc.docstatus != 1 or doc.status != "Handed Over":
		frappe.throw(_("Only handed-over shifts can be acknowledged."))
	doc.db_set({"status": "Acknowledged", "acknowledged_at": now_datetime()})
	return doc.status
