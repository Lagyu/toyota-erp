"""Daily plant schedule and mixed-model sequence per line, with an append-only
rescheduling audit (ERP-03).

``build`` turns the line's released Work Orders for a day into a leveled sequence
(goal chasing over model/drivetrain mix), slots each unit by takt time across the
shift pattern, and reports capacity conflicts. Released schedules change only with a
reason, and every change (resequence, add/remove, release, freeze, reopen) is kept
in Plant Schedule Change with before/after snapshots.
"""

from __future__ import annotations

import datetime as dt
import json

import frappe
from frappe import _
from frappe.utils import add_to_date, get_time, getdate, now_datetime

from automotive_core.access.plant_scope import get_scoped_doc
from automotive_manufacturing.engine import sequencing
from automotive_manufacturing.production_planning.snapshot import working_days

OPEN_WO = ("Not Started", "In Process", "Material Transferred")


def mix_key(item_code: str) -> str:
	template = frappe.get_cached_value("Item", item_code, "variant_of") or item_code
	drivetrain = frappe.db.get_value("Item Variant Attribute", {"parent": item_code, "attribute": "Drivetrain"},
		"attribute_value")
	return f"{template}:{drivetrain}" if drivetrain else template


def _units(line: str, day) -> list[sequencing.Unit]:
	orders = frappe.get_all(
		"Work Order",
		filters={"automotive_production_line": line, "docstatus": 1, "status": ("in", OPEN_WO),
			"planned_start_date": ("between", (f"{day} 00:00:00", f"{day} 23:59:59"))},
		fields=["name", "production_item", "qty", "produced_qty"],
		order_by="name asc",
	)
	units = []
	for wo in orders:
		key = mix_key(wo.production_item)
		for index in range(int(wo.qty - wo.produced_qty)):
			units.append(sequencing.Unit(f"{wo.name}#{index + 1:03d}", key))
	return units


def _slot_times(line_doc, day: dt.date, count: int) -> list[tuple[str, dt.datetime, dt.datetime]]:
	pattern = frappe.get_cached_doc("Shift Pattern", line_doc.shift_pattern)
	takt = dt.timedelta(seconds=float(line_doc.takt_time_seconds))
	slots, shifts = [], sorted(pattern.shifts, key=lambda s: str(s.start_time))
	for shift in shifts:
		start = dt.datetime.combine(day, get_time(shift.start_time))
		minutes = float(shift.available_minutes)
		capacity = int(minutes * 60 // takt.total_seconds())
		current = start
		for _i in range(capacity):
			if len(slots) == count:
				return slots
			slots.append((shift.shift_code, current, current + takt))
			current += takt
	while len(slots) < count:  # over capacity: queue after the last shift (reported as conflict)
		last = slots[-1][2] if slots else dt.datetime.combine(day, dt.time(6, 0))
		slots.append(("OT", last, last + takt))
	return slots


def _snapshot(doc) -> str:
	return json.dumps([{"seq": s.sequence_no, "wo": s.work_order, "unit": s.vehicle_unit} for s in doc.slots])


def log_change(doc, change_type: str, reason: str | None, before: str | None) -> None:
	entry = frappe.get_doc({"doctype": "Plant Schedule Change", "plant_schedule": doc.name, "change_type": change_type,
		"changed_by": frappe.session.user, "changed_at": now_datetime(), "reason": reason, "before_json": before,
		"after_json": _snapshot(doc)})
	entry.flags.ignore_permissions = True
	entry.insert()


@frappe.whitelist(methods=["POST"])
def build(production_line: str, schedule_date: str, reason: str | None = None) -> str:
	line = get_scoped_doc("Production Line", production_line)  # a line of the caller's own plant
	day = getdate(schedule_date)
	name = frappe.db.get_value("Plant Schedule", {"production_line": production_line, "schedule_date": day}, "name")
	# the schedule is saved past the DocType permissions (slots are the service's): the caller's role
	# must be one that may create schedules, or change an existing one
	frappe.has_permission("Plant Schedule", "write" if name else "create", throw=True)
	doc = frappe.get_doc("Plant Schedule", name) if name else frappe.new_doc("Plant Schedule")
	before = _snapshot(doc) if name else None
	if name and doc.status in ("Frozen", "Completed"):
		frappe.throw(_("Schedule {0} is {1}.").format(name, doc.status))
	if name and doc.status == "Released" and not (reason or "").strip():
		frappe.throw(_("Rebuilding a released schedule needs a reason."))
	units = sequencing.goal_chasing(_units(production_line, day))
	is_working = day.isoformat() in set(working_days(line.plant, day, day))
	capacity = int(line.max_units_per_day or 0) if is_working else 0
	times = _slot_times(line, day, len(units))
	doc.update({
		"plant": line.plant,
		"production_line": production_line,
		"schedule_date": day,
		"takt_time_seconds": line.takt_time_seconds,
		"available_minutes": frappe.get_cached_value("Shift Pattern", line.shift_pattern, "available_minutes_per_day")
			if is_working else 0,
		"capacity_units": capacity,
		"planned_units": len(units),
		"leveling_score": sequencing.leveling_deviation(units),
	})
	vins: dict[str, list[str]] = {}
	slots = []
	for seq, (unit, (shift, start, end)) in enumerate(zip(units, times, strict=True), start=1):
		wo = unit.ref.split("#")[0]
		if wo not in vins:
			vins[wo] = frappe.get_all("Vehicle Unit", filters={"work_order": wo, "production_status": "Planned"},
				pluck="name", order_by="build_sequence asc")
		slots.append({
			"sequence_no": seq,
			"work_order": wo,
			"item_code": frappe.db.get_value("Work Order", wo, "production_item"),
			"mix_key": unit.mix_key,
			"shift_code": shift,
			"planned_start": start,
			"planned_end": end,
			"vehicle_unit": vins[wo].pop(0) if vins[wo] else None,
			"slot_status": "Planned",
		})
	doc.set("slots", slots)
	conflicts = []
	if not is_working and units:
		conflicts.append(_("{0} is not a working day for this line.").format(day))
	if len(units) > capacity:
		conflicts.append(_("{0} units planned for a capacity of {1}.").format(len(units), capacity))
	doc.conflicts = "\n".join(conflicts) or None
	doc.flags.automotive_schedule_service = True
	doc.flags.ignore_permissions = True
	doc.save()
	if name:
		log_change(doc, "Resequence", reason, before)
	return doc.name


@frappe.whitelist(methods=["POST"])
def set_status(name: str, status: str, reason: str | None = None) -> str:
	transitions = {("Draft", "Released"): "Release", ("Released", "Frozen"): "Freeze", ("Frozen", "Released"): "Reopen",
		("Released", "Draft"): "Reopen", ("Frozen", "Completed"): "Freeze", ("Released", "Completed"): "Freeze"}
	doc = frappe.get_doc("Plant Schedule", name)
	doc.check_permission("write")
	change = transitions.get((doc.status, status))
	if not change:
		frappe.throw(_("Cannot move schedule from {0} to {1}.").format(doc.status, status))
	if change == "Reopen" and not (reason or "").strip():
		frappe.throw(_("Reopening needs a reason."))
	before = _snapshot(doc)
	doc.status = status
	doc.flags.automotive_schedule_service = True
	doc.save()
	log_change(doc, change, reason, before)
	return doc.status


@frappe.whitelist(methods=["POST"])
def resequence(name: str, order: str | list, reason: str) -> str:
	"""order: list of slot sequence numbers in the new order."""
	doc = frappe.get_doc("Plant Schedule", name)
	doc.check_permission("write")
	if doc.status not in ("Draft", "Released"):
		frappe.throw(_("Schedule {0} is {1}.").format(name, doc.status))
	if doc.status == "Released" and not (reason or "").strip():
		frappe.throw(_("Resequencing a released schedule needs a reason."))
	order = json.loads(order) if isinstance(order, str) else order
	current = {s.sequence_no: s for s in doc.slots}
	if sorted(order) != sorted(current):
		frappe.throw(_("The new order must contain every slot exactly once."))
	before = _snapshot(doc)
	line = frappe.get_cached_doc("Production Line", doc.production_line)
	times = _slot_times(line, getdate(doc.schedule_date), len(order))
	reordered = []
	for seq, (old_no, (shift, start, end)) in enumerate(zip(order, times, strict=True), start=1):
		slot = current[old_no]
		reordered.append({**{k: slot.get(k) for k in ("work_order", "item_code", "mix_key", "vehicle_unit",
			"slot_status")}, "sequence_no": seq, "shift_code": shift, "planned_start": start, "planned_end": end})
	doc.set("slots", reordered)
	doc.leveling_score = sequencing.leveling_deviation(
		[sequencing.Unit(s["work_order"], s["mix_key"]) for s in reordered])
	doc.flags.automotive_schedule_service = True
	doc.save()
	log_change(doc, "Resequence", reason, before)
	return doc.name


def mark_built(vin: str) -> None:
	frappe.db.sql("update `tabPlant Schedule Slot` set slot_status='Built' where vehicle_unit=%s", vin)


def validate(doc) -> None:
	if not doc.is_new() and not doc.flags.get("automotive_schedule_service"):
		before = doc.get_doc_before_save()
		if before and before.status in ("Released", "Frozen", "Completed") and _snapshot(before) != _snapshot(doc):
			raise frappe.PermissionError(_("Change a released schedule through Resequence (with a reason)."))
