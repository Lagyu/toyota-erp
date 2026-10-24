# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Weekly production plan (ERP-03.3): the master production schedule by ISO week and line.
For each week from the chosen Monday: the dealer orders due (firm demand for the vehicles the
line builds), the units the work orders plan to start and those built, and the line's
capacity (its daily rate on the plant's working days). A week planned beyond capacity, or
short of its firm orders, is flagged for the planner."""

import datetime as dt
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, nowdate

from automotive_core.analytics import scope
from automotive_manufacturing.production_planning.snapshot import _line_for, working_days

MAX_WEEKS = 26


def execute(filters=None):
	filters = frappe._dict(filters or {})
	start = getdate(filters.from_date or nowdate())
	start -= dt.timedelta(days=start.weekday())  # the Monday of the chosen week
	weeks = min(max(cint(filters.weeks) or 8, 1), MAX_WEEKS)
	end = start + dt.timedelta(days=7 * weeks - 1)
	columns = [
		{"fieldname": "week", "label": _("Week"), "fieldtype": "Date", "width": 100},
		{
			"fieldname": "production_line",
			"label": _("Production Line"),
			"fieldtype": "Link",
			"options": "Production Line",
			"width": 120,
		},
		{"fieldname": "plant", "label": _("Plant"), "fieldtype": "Link", "options": "Plant", "width": 100},
		{"fieldname": "firm_orders", "label": _("Firm Orders"), "fieldtype": "Float", "width": 100},
		{"fieldname": "planned_qty", "label": _("Planned"), "fieldtype": "Float", "width": 90},
		{"fieldname": "produced_qty", "label": _("Built"), "fieldtype": "Float", "width": 80},
		{"fieldname": "capacity", "label": _("Capacity"), "fieldtype": "Float", "width": 90},
		{"fieldname": "load_percent", "label": _("Load %"), "fieldtype": "Percent", "width": 90},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 170},
	]
	companies = scope.companies(filters)
	plants = scope.plants(filters, company_list=companies)
	lines = frappe.get_all(
		"Production Line",
		filters={"plant": ("in", plants or [""])},
		fields=["name", "plant", "max_units_per_day"],
		order_by="name",
	)
	if not lines:
		return columns, []
	line_names = [ln.name for ln in lines]
	planned = defaultdict(lambda: [0.0, 0.0])
	for row in frappe.db.sql(
		"""select wo.automotive_production_line line,
			date_sub(date(wo.planned_start_date), interval weekday(wo.planned_start_date) day) week,
			sum(wo.qty) qty, sum(wo.produced_qty) produced
		from `tabWork Order` wo
		where wo.docstatus < 2 and wo.status != 'Stopped' and wo.automotive_production_line in %(lines)s
		and date(wo.planned_start_date) between %(start)s and %(end)s
		group by line, week""",
		{"lines": tuple(line_names), "start": start, "end": end},
		as_dict=True,
	):
		planned[(row.line, getdate(row.week))] = [flt(row.qty), flt(row.produced)]
	firm = defaultdict(float)
	line_of = {}
	for row in frappe.db.sql(
		"""select soi.item_code, w.automotive_plant plant, soi.delivery_date,
			soi.stock_qty - soi.delivered_qty * soi.conversion_factor qty
		from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
		join `tabWarehouse` w on w.name = soi.warehouse
		where so.docstatus = 1 and so.status not in ('Closed', 'Completed', 'On Hold') and so.company in %(companies)s
		and w.automotive_plant in %(plants)s and soi.delivery_date between %(start)s and %(end)s
		and soi.stock_qty > soi.delivered_qty * soi.conversion_factor""",
		{"companies": scope.sql_in(companies), "plants": scope.sql_in(plants), "start": start, "end": end},
		as_dict=True,
	):
		key = (row.item_code, row.plant)
		if key not in line_of:
			line_of[key] = _line_for(row.item_code, row.plant)
		if line_of[key]:
			day = getdate(row.delivery_date)
			firm[(line_of[key], day - dt.timedelta(days=day.weekday()))] += flt(row.qty)
	data = []
	for week_no in range(weeks):
		monday = start + dt.timedelta(days=7 * week_no)
		for line in lines:
			days = len(working_days(line.plant, monday, monday + dt.timedelta(days=6)))
			capacity = cint(line.max_units_per_day) * days
			qty, produced = planned[(line.name, monday)]
			orders = firm[(line.name, monday)]
			flags = []
			if qty > capacity:
				flags.append(_("Over capacity"))
			if qty < orders:
				flags.append(_("Short of firm orders"))
			data.append(
				{
					"week": monday,
					"production_line": line.name,
					"plant": line.plant,
					"firm_orders": orders,
					"planned_qty": qty,
					"produced_qty": produced,
					"capacity": capacity,
					"load_percent": round(qty / capacity * 100, 1) if capacity else None,
					"status": ", ".join(flags) or _("Within capacity"),
				}
			)
	return columns, data
