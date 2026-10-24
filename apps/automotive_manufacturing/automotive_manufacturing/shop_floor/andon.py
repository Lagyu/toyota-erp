"""Andon lifecycle with jidoka stop and restart authorisation (ERP-04, J-05).

Raised -> Acknowledged -> Contained -> Restarted -> Closed.

* A quality reason raises a Quality Hold on the work order / station (and on the
  batch or serial when given): affected work cannot proceed (holds.py).
* Restart of a quality stop needs a Quality Manager who did not raise the event, and
  the event's hold must be released or dispositioned; other stops need a supervisor.
* A line stop's duration is booked as a Downtime Entry on the line's workstation, or on the
  stopping station's when another stop already holds the line's for that time.
* The minutes a line stood still are the union of its stops' windows (stopped_minutes).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime, time_diff_in_seconds

from automotive_core.access.plant_scope import get_scoped_doc
from automotive_core.governance import audit
from automotive_manufacturing.quality_control.holds import raise_hold

STOP_REASONS = {
	"Equipment": "Machine malfunction",
	"Process": "Machine operator errors",
}
RESPONDERS = (
	"Production Supervisor",
	"Plant Director",
	"Quality Manager",
	"Quality Inspector",
	"System Manager",
)


def validate(doc) -> None:
	line = frappe.get_cached_doc("Production Line", doc.production_line)
	doc.plant = line.plant
	if doc.workstation not in {s.workstation for s in line.stations} | {line.default_workstation}:
		frappe.throw(_("Station {0} is not on line {1}.").format(doc.workstation, doc.production_line))
	if doc.is_new():
		doc.status = "Raised"
		doc.raised_by = frappe.session.user
		doc.raised_at = now_datetime()
	elif doc.has_value_changed("status") and not doc.flags.get("automotive_transition"):
		raise frappe.PermissionError(
			_("Andon status changes through its actions (acknowledge, contain, restart, close).")
		)


def after_insert(doc) -> None:
	reason = frappe.get_cached_doc("Andon Reason Code", doc.reason_code)
	company = frappe.get_cached_value("Plant", doc.plant, "company")
	if reason.raises_quality_hold:
		if doc.work_order:
			hold = raise_hold(
				"Work Order",
				_("Andon {0}: {1}").format(doc.name, reason.description),
				company,
				source=doc,
				plant=doc.plant,
				work_order=doc.work_order,
				item=doc.item_code,
			)
		else:
			hold = raise_hold(
				"Workstation",
				_("Andon {0}: {1}").format(doc.name, reason.description),
				company,
				source=doc,
				plant=doc.plant,
				workstation=doc.workstation,
			)
		doc.db_set("quality_hold", hold)
		for scope, field, value in (
			("Batch", "batch_no", doc.batch_no),
			("Serial No", "serial_no", doc.serial_no),
		):
			if value:
				raise_hold(
					scope,
					_("Andon {0}: suspect {1}").format(doc.name, value),
					company,
					source=doc,
					plant=doc.plant,
					item=doc.item_code,
					**{field: value},
				)
	audit.record(
		"andon.raised",
		doc.doctype,
		doc.name,
		{
			"line": doc.production_line,
			"station": doc.workstation,
			"reason": doc.reason_code,
			"type": doc.stop_type,
		},
		company=company,
	)


def _transition(doc, status: str, **values) -> None:
	doc.update(values)
	doc.status = status
	doc.flags.automotive_transition = True
	doc.save(ignore_permissions=True)
	audit.record(
		f"andon.{status.lower()}",
		doc.doctype,
		doc.name,
		{k: str(v) for k, v in values.items()},
		company=frappe.get_cached_value("Plant", doc.plant, "company"),
	)


@frappe.whitelist(methods=["POST"])
def acknowledge(name: str) -> str:
	frappe.only_for(RESPONDERS)
	doc = get_scoped_doc("Andon Event", name)
	if doc.status != "Raised":
		frappe.throw(_("Andon {0} is already {1}.").format(name, doc.status))
	_transition(
		doc,
		"Acknowledged",
		acknowledged_at=now_datetime(),
		assigned_to=doc.assigned_to or frappe.session.user,
	)
	return doc.status


@frappe.whitelist(methods=["POST"])
def contain(name: str, action: str) -> str:
	frappe.only_for(RESPONDERS)
	doc = get_scoped_doc("Andon Event", name)
	if doc.status not in ("Raised", "Acknowledged"):
		frappe.throw(_("Andon {0} is {1}.").format(name, doc.status))
	if not (action or "").strip():
		frappe.throw(_("Describe the containment action."))
	values = {"containment_action": action, "contained_at": now_datetime()}
	if not doc.acknowledged_at:
		values["acknowledged_at"] = now_datetime()
	if doc.category == "Quality" and not doc.nonconformance:
		values["nonconformance"] = _open_nonconformance(doc, action)
	_transition(doc, "Contained", **values)
	return doc.status


def _open_nonconformance(doc, action: str) -> str:
	"""The case for this stop: an open one already raised for the same batch or serial (an
	inspection usually finds it first), else a new one."""
	from automotive_manufacturing.quality_control.inspections import _ensure_procedure

	for field, value in (("automotive_batch_no", doc.batch_no), ("automotive_serial_no", doc.serial_no)):
		existing = (
			frappe.db.get_value("Non Conformance", {field: value, "status": "Open"}, "name")
			if value
			else None
		)
		if existing:
			if not frappe.db.get_value("Non Conformance", existing, "automotive_andon_event"):
				frappe.db.set_value("Non Conformance", existing, "automotive_andon_event", doc.name)
			return existing
	nc = frappe.get_doc(
		{
			"doctype": "Non Conformance",
			"subject": _("Andon {0} at {1}").format(doc.reason_code, doc.workstation),
			"procedure": _ensure_procedure(),
			"status": "Open",
			"details": doc.description or doc.reason_code,
			"corrective_action": action,
			"automotive_source": "In-Process",
			"automotive_company": frappe.get_cached_value("Plant", doc.plant, "company"),
			"automotive_plant": doc.plant,
			"automotive_item": doc.item_code,
			"automotive_batch_no": doc.batch_no,
			"automotive_serial_no": doc.serial_no,
			"automotive_quality_hold": doc.quality_hold,
			"automotive_andon_event": doc.name,
		}
	)
	nc.flags.ignore_permissions = True
	nc.insert()
	return nc.name


@frappe.whitelist(methods=["POST"])
def authorize_restart(name: str, note: str | None = None) -> str:
	doc = get_scoped_doc("Andon Event", name)
	user = frappe.session.user
	# a quality stop restarts on quality's word, any other on a responder's
	if doc.category == "Quality":
		frappe.only_for(("Quality Manager", "System Manager"))
	else:
		frappe.only_for(RESPONDERS)
	if doc.status not in ("Acknowledged", "Contained"):
		frappe.throw(
			_("Andon {0} must be acknowledged/contained before restart (now {1}).").format(name, doc.status)
		)
	if doc.category == "Quality":
		if doc.status != "Contained":
			frappe.throw(_("A quality stop needs a recorded containment before restart."))
		if doc.quality_hold and frappe.db.get_value("Quality Hold", doc.quality_hold, "status") == "Active":
			frappe.throw(
				_("Quality hold {0} is still active; release or disposition it first (jidoka).").format(
					doc.quality_hold
				)
			)
	if user == doc.raised_by and user != "Administrator":
		raise frappe.PermissionError(_("The person who raised the stop cannot authorise the restart."))
	restarted = now_datetime()
	minutes = round(time_diff_in_seconds(restarted, doc.raised_at) / 60.0, 2)
	values = {"restart_authorized_by": user, "restarted_at": restarted, "duration_minutes": minutes}
	if doc.stop_type == "Line Stop":
		values["downtime_entry"] = _book_downtime(doc, restarted, note)
	_transition(doc, "Restarted", **values)
	return doc.status


def _book_downtime(doc, restarted, note: str | None) -> str | None:
	operator = frappe.db.get_value("Employee", {"user_id": doc.raised_by}, "name") or frappe.db.get_value(
		"Plant Assignment", {"production_line": doc.production_line, "status": "Active"}, "employee"
	)
	if not operator:
		return None
	line = frappe.get_cached_doc("Production Line", doc.production_line)
	# ERPNext refuses overlapping entries on one workstation: when another station's stop holds
	# the line's workstation for part of this window (two cords pulled at once), the stopping
	# station's own workstation takes the entry, and the restart never fails for it
	for workstation in dict.fromkeys(w for w in (line.default_workstation, doc.workstation) if w):
		if not frappe.db.exists(
			"Downtime Entry",
			{"workstation": workstation, "from_time": ("<", restarted), "to_time": (">", doc.raised_at)},
		):
			break
	else:
		return None
	entry = frappe.get_doc(
		{
			"doctype": "Downtime Entry",
			"workstation": workstation,
			"operator": operator,
			"from_time": doc.raised_at,
			"to_time": restarted,
			"stop_reason": STOP_REASONS.get(doc.category, "Other"),
			"remarks": _("Andon {0} ({1}): {2}").format(
				doc.name, doc.reason_code, note or doc.containment_action or ""
			),
			"automotive_andon_event": doc.name,
			"automotive_production_line": doc.production_line,
		}
	)
	entry.flags.ignore_permissions = True
	entry.insert()
	return entry.name


def stopped_minutes(production_line: str, start, end) -> float:
	"""Minutes ``production_line`` stood still for its restarted line stops raised in
	[start, end): the union of their raise-to-restart windows, so that stops overlapping in
	time (two stations' cords) count once."""
	windows = frappe.db.sql(
		"""select raised_at, restarted_at from `tabAndon Event` where production_line = %s
		and stop_type = 'Line Stop' and restarted_at is not null and raised_at >= %s and raised_at < %s
		order by raised_at""",
		(production_line, start, end),
	)
	seconds, reach = 0.0, None
	for raised, restarted in windows:
		raised, restarted = get_datetime(raised), get_datetime(restarted)
		begin = raised if reach is None else max(raised, reach)
		if restarted > begin:
			seconds += (restarted - begin).total_seconds()
		reach = restarted if reach is None else max(reach, restarted)
	return round(seconds / 60.0, 2)


@frappe.whitelist(methods=["POST"])
def close(name: str) -> str:
	frappe.only_for(RESPONDERS)
	doc = get_scoped_doc("Andon Event", name)
	if doc.stop_type == "Line Stop" and doc.status != "Restarted":
		frappe.throw(_("A line stop closes after the restart."))
	if doc.stop_type == "Call" and doc.status == "Raised":
		frappe.throw(_("Acknowledge the call first."))
	_transition(doc, "Closed")
	return doc.status
