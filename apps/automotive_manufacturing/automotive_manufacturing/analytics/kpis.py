"""Manufacturing KPIs (ERP-12): production attainment, line stops, shortage risk,
WIP value and OEE. Each reads ledgers/documents only and has an independent
reconciliation check."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, flt

from automotive_core.analytics.kpi import KpiResult, ratio

DEFINITIONS = [
	{
		"kpi_code": "MFG-ATTAIN",
		"kpi_name": "Production Attainment",
		"domain": "Production",
		"unit": "Percent",
		"direction": "Higher Is Better",
		"target_value": 98,
		"refresh_policy": "Daily",
		"compute_method": "automotive_manufacturing.analytics.kpis.production_attainment",
		"reconciliation_method": "automotive_manufacturing.analytics.kpis.reconcile_attainment",
		"owner_role": "Plant Director",
		"formula": "Units produced by Manufacture entries in period / units planned in Plant Schedules for the period x 100",
		"source_doctypes": "Stock Entry (Manufacture), Plant Schedule, Plant Schedule Slot",
		"authorization_filter": "Company and Plant User Permissions of the caller.",
		"reconciliation_check": "Produced units equal the sum of finished-goods SLE qty from Manufacture vouchers.",
	},
	{
		"kpi_code": "MFG-LINESTOP",
		"kpi_name": "Line Stop Minutes",
		"domain": "Production",
		"unit": "Hours",
		"direction": "Lower Is Better",
		"target_value": 10,
		"refresh_policy": "Daily",
		"compute_method": "automotive_manufacturing.analytics.kpis.line_stop_hours",
		"reconciliation_method": "automotive_manufacturing.analytics.kpis.reconcile_line_stops",
		"owner_role": "Plant Director",
		"formula": "Sum of Andon line-stop durations (raised to restart) in period, in hours",
		"source_doctypes": "Andon Event, Downtime Entry",
		"authorization_filter": "Company and Plant User Permissions of the caller.",
		"reconciliation_check": "Andon stop hours equal the Downtime Entries booked from those andons, and the KPI "
		"equals the booked downtime plus the stops nobody booked.",
	},
	{
		"kpi_code": "MFG-SHORTAGE",
		"kpi_name": "Shortage Risk (open exceptions)",
		"domain": "Production",
		"unit": "Count",
		"direction": "Lower Is Better",
		"target_value": 0,
		"refresh_policy": "Daily",
		"compute_method": "automotive_manufacturing.analytics.kpis.shortage_risk",
		"reconciliation_method": "automotive_manufacturing.analytics.kpis.reconcile_shortage_risk",
		"owner_role": "Production Planner",
		"formula": "Shortage lines in the latest completed Planning Run per plant + escalated Kanban signals",
		"source_doctypes": "Planning Run, Planning Run Shortage, Kanban Signal",
		"authorization_filter": "Company and Plant User Permissions of the caller.",
		"reconciliation_check": "Run shortage count field equals the number of shortage rows.",
	},
	{
		"kpi_code": "MFG-WIP",
		"kpi_name": "Work In Progress Value",
		"domain": "Inventory",
		"unit": "Amount",
		"direction": "Lower Is Better",
		"target_value": 0,
		"refresh_policy": "Daily",
		"compute_method": "automotive_manufacturing.analytics.kpis.wip_value",
		"reconciliation_method": "automotive_manufacturing.analytics.kpis.reconcile_wip",
		"owner_role": "Group Controller",
		"formula": "Stock value in Work In Progress warehouses at period end (SLE stock_value_difference sum)",
		"source_doctypes": "Stock Ledger Entry, Warehouse, GL Entry",
		"authorization_filter": "Company User Permission; plant filter when given.",
		"reconciliation_check": "SLE value equals the GL balance of the WIP warehouses' stock accounts (±1).",
	},
	{
		"kpi_code": "MFG-OEE",
		"kpi_name": "Overall Equipment Effectiveness (line)",
		"domain": "Production",
		"unit": "Percent",
		"direction": "Higher Is Better",
		"target_value": 85,
		"refresh_policy": "Daily",
		"compute_method": "automotive_manufacturing.analytics.kpis.oee",
		"reconciliation_method": "automotive_manufacturing.analytics.kpis.reconcile_oee",
		"owner_role": "Plant Director",
		"formula": "Availability (planned minutes - line stop minutes)/planned x Performance (units x takt / run minutes) "
		"x Quality (units released first time / units built)",
		"source_doctypes": "Production Line, Shift Pattern, Andon Event, Vehicle Unit, Quality Inspection",
		"authorization_filter": "Company and Plant User Permissions of the caller.",
		"reconciliation_check": "Each factor stays within 0..100% and the value is their product.",
	},
]


def _plant_clause(plant: str | None, column: str = "automotive_plant") -> tuple[str, dict]:
	return (f" and {column} = %(plant)s", {"plant": plant}) if plant else ("", {})


def _produced(company, from_date, to_date, plant=None) -> float:
	clause, values = _plant_clause(plant, "se.automotive_plant")
	return flt(
		frappe.db.sql(
			f"""select sum(se.fg_completed_qty) from `tabStock Entry` se
		where se.docstatus=1 and se.purpose='Manufacture' and se.company=%(company)s
		and se.posting_date between %(from)s and %(to)s {clause}""",  # noqa: S608 (constant SQL fragments; values are bound)
			{"company": company, "from": from_date, "to": to_date, **values},
		)[0][0]
	)


def production_attainment(company, from_date, to_date, plant=None) -> KpiResult:
	produced = _produced(company, from_date, to_date, plant)
	clause, values = _plant_clause(plant, "ps.plant")
	planned = flt(
		frappe.db.sql(
			f"""select sum(ps.planned_units) from `tabPlant Schedule` ps
		join `tabPlant` p on p.name = ps.plant
		where p.company=%(company)s and ps.schedule_date between %(from)s and %(to)s {clause}""",  # noqa: S608 (constant SQL fragments; values are bound)
			{"company": company, "from": from_date, "to": to_date, **values},
		)[0][0]
	)
	return KpiResult(
		ratio(produced, planned),
		produced,
		planned,
		{"company": company, "plant": plant, "from": str(from_date), "to": str(to_date)},
	)


def reconcile_attainment(company, from_date, to_date, plant, result) -> tuple[bool, str]:
	clause, values = _plant_clause(plant, "se.automotive_plant")
	sle = flt(
		frappe.db.sql(
			f"""select sum(sle.actual_qty) from `tabStock Ledger Entry` sle
		join `tabStock Entry` se on se.name = sle.voucher_no and sle.voucher_type = 'Stock Entry'
		join `tabStock Entry Detail` sed on sed.name = sle.voucher_detail_no
		where se.purpose='Manufacture' and sed.is_finished_item=1 and sle.is_cancelled=0
		and se.company=%(company)s and se.posting_date between %(from)s and %(to)s {clause}""",  # noqa: S608 (constant SQL fragments; values are bound)
			{"company": company, "from": from_date, "to": to_date, **values},
		)[0][0]
	)
	ok = abs(sle - flt(result.numerator)) < 1e-6
	return ok, f"produced {flt(result.numerator)} vs finished-goods SLE {sle}"


def _stops(company, from_date, to_date, plant=None):
	clause, values = _plant_clause(plant, "ae.plant")
	return frappe.db.sql(
		f"""select ae.name, ae.duration_minutes, ae.downtime_entry from `tabAndon Event` ae
		join `tabPlant` p on p.name = ae.plant
		where p.company=%(company)s and ae.stop_type='Line Stop' and ae.restarted_at is not null
		and date(ae.raised_at) between %(from)s and %(to)s {clause}""",  # noqa: S608 (constant SQL fragments; values are bound)
		{"company": company, "from": from_date, "to": to_date, **values},
		as_dict=True,
	)


def line_stop_hours(company, from_date, to_date, plant=None) -> KpiResult:
	stops = _stops(company, from_date, to_date, plant)
	minutes = sum(flt(s.duration_minutes) for s in stops)
	return KpiResult(
		round(minutes / 60.0, 3),
		minutes,
		len(stops),
		{"company": company, "plant": plant, "from": str(from_date), "to": str(to_date), "stops": len(stops)},
	)


def reconcile_line_stops(company, from_date, to_date, plant, result) -> tuple[bool, str]:
	stops = _stops(company, from_date, to_date, plant)
	booked = [s.downtime_entry for s in stops if s.downtime_entry]
	downtime_hours = (
		flt(
			frappe.db.sql(
				"select sum(downtime) from `tabDowntime Entry` where name in %s", [tuple(booked) or ("",)]
			)[0][0]
		)
		if booked
		else 0.0
	)
	andon_hours = sum(flt(s.duration_minutes) for s in stops if s.downtime_entry) / 60.0
	unbooked_hours = sum(flt(s.duration_minutes) for s in stops if not s.downtime_entry) / 60.0
	# the KPI from the downtime ledger: the hours booked from the stops, plus the stops nobody booked
	tolerance = 0.02 * max(len(stops), 1)
	ok = (
		abs(downtime_hours - andon_hours) <= tolerance
		and abs(flt(result.value) - (downtime_hours + unbooked_hours)) <= tolerance
	)
	return ok, (
		f"KPI {flt(result.value):.2f} h; andon {andon_hours:.2f} h vs downtime entries {downtime_hours:.2f} h "
		f"({len(booked)} booked of {len(stops)}, {unbooked_hours:.2f} h unbooked)"
	)


def shortage_risk(company, from_date, to_date, plant=None) -> KpiResult:
	filters = {
		"company": company,
		"status": ("in", ("Completed", "Released")),
		"replay_of": ("is", "not set"),
	}
	if plant:
		filters["plant"] = plant
	plants = [plant] if plant else frappe.get_all("Plant", filters={"company": company}, pluck="name")
	total, runs = 0, []
	for p in plants:
		latest = frappe.get_all(
			"Planning Run",
			filters={**filters, "plant": p},
			order_by="creation desc",
			limit=1,
			fields=["name", "shortage_count"],
		)
		if latest:
			runs.append(latest[0].name)
			total += int(latest[0].shortage_count or 0)
	escalated = frappe.db.count("Kanban Signal", {"status": "Escalated", "plant": ("in", plants or [""])})
	return KpiResult(total + escalated, total, escalated, {"runs": runs, "escalated_signals": escalated})


def reconcile_shortage_risk(company, from_date, to_date, plant, result) -> tuple[bool, str]:
	runs = result.inputs.get("runs", [])
	rows = frappe.db.count("Planning Run Shortage", {"parent": ("in", runs or [""])})
	ok = rows == int(result.numerator or 0)
	return ok, f"shortage rows {rows} vs run counters {int(result.numerator or 0)}"


def _wip_warehouses(company, plant=None) -> list[str]:
	filters = {"company": company, "is_group": 0, "automotive_warehouse_role": "Work In Progress"}
	if plant:
		filters["automotive_plant"] = plant
	return frappe.get_all("Warehouse", filters=filters, pluck="name")


def wip_value(company, from_date, to_date, plant=None) -> KpiResult:
	warehouses = _wip_warehouses(company, plant)
	value = (
		flt(
			frappe.db.sql(
				"""select sum(stock_value_difference) from `tabStock Ledger Entry`
		where is_cancelled=0 and warehouse in %s and posting_date <= %s""",
				(tuple(warehouses) or ("",), to_date),
			)[0][0]
		)
		if warehouses
		else 0.0
	)
	return KpiResult(round(value, 2), value, None, {"warehouses": warehouses, "to": str(to_date)})


def reconcile_wip(company, from_date, to_date, plant, result) -> tuple[bool, str]:
	warehouses = result.inputs.get("warehouses", [])
	accounts = sorted(
		{
			frappe.get_cached_value("Warehouse", w, "account")
			or frappe.get_cached_value("Company", company, "default_inventory_account")
			for w in warehouses
		}
	)
	if not accounts:
		return True, "no WIP warehouses: not applicable"
	shared = frappe.get_all(
		"Warehouse",
		filters={"account": ("in", accounts), "name": ("not in", warehouses or [""])},
		pluck="name",
		limit=1,
	)
	if shared:
		return (
			True,
			"WIP warehouses share their stock account with other warehouses; ledger-level check not applicable",
		)
	gl = flt(
		frappe.db.sql(
			"""select sum(debit - credit) from `tabGL Entry` where is_cancelled=0 and account in %s and posting_date <= %s""",
			(tuple(accounts), to_date),
		)[0][0]
	)
	ok = abs(gl - flt(result.numerator)) <= 1
	return ok, f"SLE {flt(result.numerator):.2f} vs GL {gl:.2f}"


def oee(company, from_date, to_date, plant=None) -> KpiResult:
	lines = frappe.get_all(
		"Production Line",
		filters={"plant": plant} if plant else {},
		fields=["name", "plant", "takt_time_seconds", "shift_pattern"],
	)
	lines = [ln for ln in lines if frappe.get_cached_value("Plant", ln.plant, "company") == company]
	planned_min = run_min = ideal_min = 0.0
	built = good = 0
	for line in lines:
		schedules = frappe.get_all(
			"Plant Schedule",
			filters={"production_line": line.name, "schedule_date": ("between", (from_date, to_date))},
			fields=["available_minutes"],
		)
		line_planned = sum(flt(s.available_minutes) for s in schedules)
		if not line_planned:
			continue
		stops = sum(
			flt(s.duration_minutes)
			for s in frappe.get_all(
				"Andon Event",
				filters={
					"production_line": line.name,
					"stop_type": "Line Stop",
					"raised_at": ("between", (from_date, add_days(to_date, 1))),
				},
				fields=["duration_minutes"],
			)
		)
		units = frappe.db.count(
			"Vehicle Unit",
			{"production_line": line.name, "built_at": ("between", (from_date, add_days(to_date, 1)))},
		)
		released_first_time = frappe.db.count(
			"Vehicle Unit",
			{
				"production_line": line.name,
				"quality_status": "Released",
				"built_at": ("between", (from_date, add_days(to_date, 1))),
			},
		)
		planned_min += line_planned
		run_min += max(line_planned - stops, 0)
		ideal_min += units * flt(line.takt_time_seconds) / 60.0
		built += units
		good += released_first_time
	availability = run_min / planned_min if planned_min else 0
	performance = min(ideal_min / run_min, 1.0) if run_min else 0
	quality = good / built if built else 0
	value = round(availability * performance * quality * 100, 2) if planned_min else None
	return KpiResult(
		value,
		ideal_min,
		planned_min,
		{
			"availability": availability,
			"performance": performance,
			"quality": quality,
			"built": built,
			"lines": [ln.name for ln in lines],
		},
	)


def reconcile_oee(company, from_date, to_date, plant, result) -> tuple[bool, str]:
	factors = [result.inputs.get(k, 0) for k in ("availability", "performance", "quality")]
	in_range = all(0 <= f <= 1.0000001 for f in factors)
	# the value is the product of its factors (none without planned time)
	expected = round(factors[0] * factors[1] * factors[2] * 100, 2) if flt(result.denominator) else None
	consistent = (
		expected == result.value
		if expected is None or result.value is None
		else abs(expected - flt(result.value)) <= 0.01
	)
	ok = in_range and consistent
	return ok, "OEE {} from A={:.3f} P={:.3f} Q={:.3f}, built={}".format(
		result.value, *factors, result.inputs.get("built")
	)
