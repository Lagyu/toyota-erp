"""Jidoka-style quality holds and their enforcement (ERP-04, ERP-07, J-05).

An *Active* Quality Hold stops affected work and material everywhere it could move:

=====================  ==========================================================
Stock Entry             no row may move held items/batches/serials, except moves
                        into a quarantine or scrap warehouse and entries created by
                        a Quality Disposition; manufacture/transfer for a held work
                        order, line or workstation is blocked
Job Card                no time logs / completion on a held work order, workstation
                        or line
Work Order              cannot be submitted (released) while held
Purchase Receipt        goods from a held supplier must be received into quarantine
Delivery Note,          held serials/batches/items cannot be picked, shipped or
Pick List,              invoiced with stock; vehicles must be quality *Released*
Sales Invoice
=====================  ==========================================================

Release needs every release condition ticked and a Quality Manager who did not
raise the hold (maker/checker); release and every change go to the audit chain.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from automotive_core.governance import audit

DEFAULT_RELEASE_CHECKS = [
	"Containment completed (suspect stock segregated)",
	"Inspection / test of suspect stock passed or dispositioned",
	"Root cause identified or interim action in place",
]


class QualityHoldError(frappe.ValidationError):
	pass


def _active(filters: dict) -> list[dict]:
	return frappe.get_all("Quality Hold", filters={"status": "Active", **filters},
		fields=["name", "hold_scope", "item", "batch_no", "serial_no", "reason"])


def active_holds_for(item: str | None = None, batch: str | None = None, serials: list[str] | None = None,
		work_order: str | None = None, workstation: str | None = None, line: str | None = None,
		supplier: str | None = None) -> list[dict]:
	found = []
	if batch:
		found += _active({"hold_scope": "Batch", "batch_no": batch})
	for serial in serials or []:
		found += _active({"hold_scope": "Serial No", "serial_no": serial})
	if item:
		found += _active({"hold_scope": "Item", "item": item})
	if work_order:
		found += _active({"hold_scope": "Work Order", "work_order": work_order})
	if workstation:
		found += _active({"hold_scope": "Workstation", "workstation": workstation})
	if line:
		found += _active({"hold_scope": "Production Line", "production_line": line})
	if supplier:
		found += _active({"hold_scope": "Supplier", "supplier": supplier})
	return list({h.name: h for h in found}.values())


def _serials(row) -> list[str]:
	return [s.strip() for s in (row.get("serial_no") or "").split("\n") if s.strip()]


def _blocked(holds: list[dict], what: str) -> None:
	if holds:
		raise QualityHoldError(
			_("{0} is under quality hold: {1}").format(what, "; ".join(f"{h.name} ({h.reason})" for h in holds))
		)


def _is_segregation_target(warehouse: str | None) -> bool:
	return bool(warehouse) and frappe.get_cached_value("Warehouse", warehouse, "automotive_warehouse_role") in (
		"Quarantine", "Scrap")


def validate_stock_entry(doc, method=None) -> None:
	if doc.docstatus == 2 or doc.get("automotive_source_doctype") == "Quality Disposition":
		return
	line = None
	if doc.work_order:
		line = frappe.db.get_value("Work Order", doc.work_order, "automotive_production_line")
		if doc.purpose in ("Manufacture", "Material Transfer for Manufacture"):
			_blocked(active_holds_for(work_order=doc.work_order, line=line), _("Work Order {0}").format(doc.work_order))
	for row in doc.items:
		if row.t_warehouse and not row.s_warehouse:
			if doc.purpose != "Manufacture" or not row.is_finished_item:
				continue  # receipts create new stock; held suppliers are handled on Purchase Receipt
		if row.s_warehouse and _is_segregation_target(row.t_warehouse):
			continue  # moving suspect stock into quarantine/scrap is always allowed
		holds = active_holds_for(item=row.item_code, batch=row.get("batch_no"), serials=_serials(row))
		_blocked(holds, _("Row {0}: {1}").format(row.idx, row.item_code))


def validate_job_card(doc, method=None) -> None:
	if doc.docstatus == 2:
		return
	time_logged = any(log.get("from_time") for log in doc.get("time_logs") or [])
	if not time_logged and not doc.get("total_completed_qty"):
		return
	line = frappe.db.get_value("Workstation", doc.workstation, "automotive_production_line") if doc.workstation else None
	_blocked(active_holds_for(work_order=doc.work_order, workstation=doc.workstation, line=line),
		_("Job Card {0}").format(doc.name or ""))


def validate_work_order(doc, method=None) -> None:
	if doc.docstatus == 1 or (doc.docstatus == 0 and doc.flags.get("in_submit")):
		_blocked(active_holds_for(item=doc.production_item, work_order=doc.name,
			line=doc.get("automotive_production_line")), _("Work Order {0}").format(doc.name))


def validate_purchase_receipt(doc, method=None) -> None:
	if doc.docstatus == 2 or doc.get("is_return"):
		return
	supplier_holds = active_holds_for(supplier=doc.supplier)
	if not supplier_holds:
		return
	for row in doc.items:
		if not _is_segregation_target(row.warehouse):
			raise QualityHoldError(
				_("Supplier {0} is under quality hold ({1}); receive row {2} into a quarantine warehouse.").format(
					doc.supplier, supplier_holds[0].name, row.idx)
			)


def validate_outbound(doc, method=None) -> None:
	if doc.docstatus == 2:
		return
	if doc.doctype == "Sales Invoice" and not doc.get("update_stock"):
		return
	rows = doc.get("locations") if doc.doctype == "Pick List" else doc.items
	if doc.get("is_return"):
		return
	for row in rows or []:
		serials = _serials(row)
		_blocked(active_holds_for(item=row.item_code, batch=row.get("batch_no"), serials=serials),
			_("Row {0}: {1}").format(row.idx, row.item_code))
		for serial in serials:
			status = frappe.db.get_value("Vehicle Unit", serial, "quality_status")
			if status and status != "Released":
				raise QualityHoldError(
					_("Row {0}: vehicle {1} is not quality-released ({2}).").format(row.idx, serial, _(status))
				)


# ---------------------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------------------


def raise_hold(scope: str, reason: str, company: str, source=None, **targets) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Quality Hold",
			"hold_scope": scope,
			"reason": reason,
			"company": company,
			"source_doctype": source.doctype if source else None,
			"source_name": source.name if source else None,
			**targets,
			"release_checks": [{"check_item": c} for c in DEFAULT_RELEASE_CHECKS],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def on_hold_insert(doc) -> None:
	doc.db_set({"raised_by": frappe.session.user, "raised_at": now_datetime()}, update_modified=False)
	_sync_vehicle_quality(doc, "On Hold")
	audit.record("quality.hold_raised", doc.doctype, doc.name,
		{"scope": doc.hold_scope, "item": doc.item, "batch": doc.batch_no, "serial": doc.serial_no,
		 "work_order": doc.work_order, "reason": doc.reason}, company=doc.company)


def validate_hold(doc) -> None:
	required = {
		"Batch": "batch_no", "Serial No": "serial_no", "Item": "item", "Work Order": "work_order",
		"Workstation": "workstation", "Production Line": "production_line", "Supplier": "supplier",
	}[doc.hold_scope]
	if not doc.get(required):
		frappe.throw(_("A {0} hold needs the {1}.").format(doc.hold_scope, doc.meta.get_label(required)))
	if doc.batch_no and not doc.item:
		doc.item = frappe.db.get_value("Batch", doc.batch_no, "item")
	if doc.serial_no and not doc.item:
		doc.item = frappe.db.get_value("Serial No", doc.serial_no, "item_code")
	if not doc.is_new() and doc.has_value_changed("status") and not doc.flags.get("automotive_release"):
		raise frappe.PermissionError(_("Use Release (or a Quality Disposition) to change a hold's status."))
	for check in doc.release_checks:
		if check.done and not check.done_by:
			check.done_by = frappe.session.user
			check.done_at = now_datetime()


@frappe.whitelist(methods=["POST"])
def release(name: str, note: str) -> str:
	doc = frappe.get_doc("Quality Hold", name)
	frappe.only_for(("Quality Manager", "System Manager"))
	if doc.status != "Active":
		frappe.throw(_("Hold {0} is {1}.").format(name, doc.status))
	if not (note or "").strip():
		frappe.throw(_("A release note is required."))
	if frappe.session.user == doc.raised_by and frappe.session.user != "Administrator":
		raise frappe.PermissionError(_("The person who raised a hold cannot release it."))
	open_checks = [c.check_item for c in doc.release_checks if not c.done]
	if open_checks:
		frappe.throw(_("Release conditions not met: {0}").format("; ".join(open_checks)))
	doc.status = "Released"
	doc.released_by = frappe.session.user
	doc.released_at = now_datetime()
	doc.release_note = note
	doc.flags.automotive_release = True
	doc.save()
	_sync_vehicle_quality(doc, None)
	audit.record("quality.hold_released", doc.doctype, doc.name, {"note": note}, company=doc.company)
	return doc.status


def close_dispositioned(name: str) -> None:
	doc = frappe.get_doc("Quality Hold", name)
	if doc.status == "Active":
		doc.status = "Dispositioned"
		doc.released_by = frappe.session.user
		doc.released_at = now_datetime()
		doc.flags.automotive_release = True
		doc.save(ignore_permissions=True)
		_sync_vehicle_quality(doc, None)


def _sync_vehicle_quality(hold, status: str | None) -> None:
	"""Serial holds move the unit to On Hold; on release it returns to its inspection state."""
	if hold.hold_scope != "Serial No" or not frappe.db.exists("Vehicle Unit", hold.serial_no):
		return
	if status:
		frappe.db.set_value("Vehicle Unit", hold.serial_no, "quality_status", status)
		return
	inspection = frappe.db.get_value("Vehicle Unit", hold.serial_no, "final_inspection")
	accepted = inspection and frappe.db.get_value("Quality Inspection", inspection, ["docstatus", "status"]) == (1, "Accepted")
	frappe.db.set_value("Vehicle Unit", hold.serial_no, "quality_status", "Released" if accepted else "Pending Inspection")
