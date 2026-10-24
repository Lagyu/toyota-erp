# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""OEE inputs by line and day (ERP-04, KPI MFG-OEE): availability from the released plant
schedule minus Andon line stops, performance from units built at takt, quality from
units released at final inspection. Same definitions as the KPI, broken down."""

import datetime as dt

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate

from automotive_core.analytics import scope


def execute(filters=None):
	filters = frappe._dict(filters or {})
	to_date = getdate(filters.to_date or nowdate())
	from_date = getdate(filters.from_date or add_days(to_date, -6))
	columns = [
		{"fieldname": "production_line", "label": _("Line"), "fieldtype": "Link", "options": "Production Line", "width": 130},
		{"fieldname": "date", "label": _("Date"), "fieldtype": "Date", "width": 100},
		{"fieldname": "available_minutes", "label": _("Planned Minutes"), "fieldtype": "Float", "width": 120},
		{"fieldname": "stop_minutes", "label": _("Line-Stop Minutes"), "fieldtype": "Float", "width": 130},
		{"fieldname": "units", "label": _("Units Built"), "fieldtype": "Int", "width": 100},
		{"fieldname": "released", "label": _("Released"), "fieldtype": "Int", "width": 90},
		{"fieldname": "availability", "label": _("Availability %"), "fieldtype": "Percent", "width": 115},
		{"fieldname": "performance", "label": _("Performance %"), "fieldtype": "Percent", "width": 115},
		{"fieldname": "quality", "label": _("Quality %"), "fieldtype": "Percent", "width": 100},
		{"fieldname": "oee", "label": _("OEE %"), "fieldtype": "Percent", "width": 90},
	]
	plants = scope.plants(filters, company_list=scope.companies(filters))
	line_filters = {"plant": ("in", plants or [""])}
	if filters.production_line:
		line_filters["name"] = filters.production_line
	data = []
	for line in frappe.get_all("Production Line", filters=line_filters, fields=["name", "takt_time_seconds"],
			order_by="name"):
		day = from_date
		while day <= to_date:
			data.append(_day(line, day))
			day = add_days(day, 1)
	data = [row for row in data if row["available_minutes"] or row["units"]]
	return columns, data


def _pct(value: float) -> float:
	return round(value * 100, 2)


def _day(line, day) -> dict:
	start, end = dt.datetime.combine(day, dt.time.min), dt.datetime.combine(day, dt.time.max)
	available = flt(frappe.db.get_value("Plant Schedule", {"production_line": line.name, "schedule_date": day},
		"available_minutes"))
	stops = flt(frappe.db.sql("""select sum(duration_minutes) from `tabAndon Event` where production_line = %s
		and stop_type = 'Line Stop' and raised_at between %s and %s""", (line.name, start, end))[0][0])
	units = frappe.db.count("Vehicle Unit", {"production_line": line.name, "built_at": ("between", (start, end))})
	released = frappe.db.count("Vehicle Unit", {"production_line": line.name, "quality_status": "Released",
		"built_at": ("between", (start, end))})
	run = max(available - stops, 0)
	ideal = units * flt(line.takt_time_seconds) / 60.0
	availability = run / available if available else 0
	performance = min(ideal / run, 1.0) if run else 0
	quality = released / units if units else 0
	return {"production_line": line.name, "date": day, "available_minutes": available, "stop_minutes": stops,
		"units": units, "released": released, "availability": _pct(availability), "performance": _pct(performance),
		"quality": _pct(quality), "oee": _pct(availability * performance * quality)}
