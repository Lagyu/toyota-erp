"""MES production reporting: build one serialized unit (or a batch) against a Work
Order with idempotent, exactly-once stock effects (ERP-04, J-01).

``report_unit_built`` posts a Manufacture Stock Entry for qty 1 using the next
reserved VIN as the finished serial, optionally with the component serials/batches
scanned at the stations (otherwise ERPNext picks them FIFO). Component consumption,
valuation, GL and genealogy all come from that single native entry. Retries with the
same Idempotency-Key return the first result (integration_hub.idempotency).
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime, nowdate, nowtime

from automotive_core.access.plant_scope import get_scoped_doc
from automotive_core.integration_hub.idempotency import idempotent
from automotive_manufacturing.traceability.vehicle_units import planned_vins

MES_ROLES = ("Production Supervisor", "Line Operator", "Manufacturing User", "System Manager")


def record_operation(
	job_card: str, qty: float, employee: str | None = None, from_time=None, to_time=None
) -> str:
	"""An operation confirmation from the station (ERP-04.1, labour record): ``qty`` units done
	on the job card's operation between ``from_time`` and ``to_time`` by ``employee``. The job
	card is submitted once its quantity is complete, which is what lets the work order post
	the units (ERPNext checks every operation is confirmed for them). Returns its status."""
	doc = frappe.get_doc("Job Card", job_card)
	if doc.docstatus != 0:
		frappe.throw(_("Job Card {0} is already {1}.").format(job_card, doc.status))
	remaining = flt(doc.for_quantity) - flt(doc.total_completed_qty)
	if flt(qty) <= 0 or flt(qty) > remaining + 1e-9:
		frappe.throw(
			_("Job Card {0}: {1} unit(s) left to confirm, not {2}.").format(job_card, remaining, qty)
		)
	doc.append(
		"time_logs",
		{
			"from_time": from_time or now_datetime(),
			"to_time": to_time or now_datetime(),
			"completed_qty": qty,
			"employee": employee,
		},
	)
	doc.save()
	if flt(doc.total_completed_qty) >= flt(doc.for_quantity) - 1e-9:
		doc.submit()
	return doc.status


def build_entry(
	work_order: str,
	qty: float = 1,
	serial_nos: list[str] | None = None,
	components: list[dict] | None = None,
	posting_date=None,
	posting_time=None,
):
	from erpnext.manufacturing.doctype.work_order.work_order import make_stock_entry

	wo = frappe.get_doc("Work Order", work_order)
	if wo.docstatus != 1 or wo.status in ("Completed", "Stopped", "Closed"):
		frappe.throw(_("Work Order {0} is {1}.").format(work_order, wo.status))
	if flt(wo.produced_qty) + flt(qty) > flt(wo.qty) + 1e-9:
		frappe.throw(
			_("Work Order {0}: only {1} left to build.").format(
				work_order, flt(wo.qty) - flt(wo.produced_qty)
			)
		)
	data = make_stock_entry(work_order, "Manufacture", qty)
	se = frappe.get_doc(data)
	if posting_date:
		se.set_posting_time = 1
		se.posting_date = getdate(posting_date)
		se.posting_time = posting_time or nowtime()
	serials = None
	if frappe.get_cached_value("Item", wo.production_item, "has_serial_no"):
		if serial_nos:
			serials = serial_nos
		elif frappe.db.exists("Vehicle Unit", {"work_order": work_order}):
			serials = planned_vins(work_order, int(qty))  # vehicles: the VINs reserved in build sequence
		# other serialized products (powertrains, engines) take the next numbers of their series
		if serials is not None and len(serials) != int(qty):
			frappe.throw(
				_("Work Order {0}: {1} serial number(s) needed, {2} available.").format(
					work_order, int(qty), len(serials)
				)
			)
	for row in se.items:
		if row.is_finished_item:
			row.use_serial_batch_fields = 1
			if serials:
				row.serial_no = "\n".join(serials)
	scanned = {}
	for comp in components or []:
		scanned.setdefault(comp["item_code"], []).append(comp)
	for row in se.items:
		if row.is_finished_item or row.item_code not in scanned:
			continue
		picks = scanned.pop(row.item_code)
		row.use_serial_batch_fields = 1
		serials = [p["serial_no"] for p in picks if p.get("serial_no")]
		if serials:
			row.serial_no = "\n".join(serials)
		batches = {p["batch_no"] for p in picks if p.get("batch_no")}
		if len(batches) > 1:
			frappe.throw(
				_("{0}: one batch per component row (got {1}).").format(row.item_code, sorted(batches))
			)
		if batches:
			row.batch_no = batches.pop()
	if scanned:
		frappe.throw(
			_("Scanned components are not in the BOM of {0}: {1}").format(work_order, sorted(scanned))
		)
	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se


@frappe.whitelist(methods=["POST"])
@idempotent("mes.report_unit_built")
def report_unit_built(
	work_order: str, components: str | list | None = None, serial_no: str | None = None
) -> dict[str, Any]:
	"""Station/MES call: one unit finished. components: [{"item_code","serial_no"|"batch_no"}]."""
	frappe.only_for(MES_ROLES)
	if isinstance(components, str):
		components = json.loads(components)
	se = build_entry(work_order, 1, [serial_no] if serial_no else None, components)
	built = [r.serial_no for r in se.items if r.is_finished_item]
	return {
		"stock_entry": se.name,
		"serial_no": (built[0] or "").strip() if built else None,
		"work_order": work_order,
		"posted_at": str(now_datetime()),
	}


@frappe.whitelist(methods=["POST"])
@idempotent("mes.report_batch_built")
def report_batch_built(work_order: str, qty: float) -> dict[str, Any]:
	frappe.only_for(MES_ROLES)
	se = build_entry(work_order, flt(qty))
	return {"stock_entry": se.name, "work_order": work_order, "qty": flt(qty)}


@frappe.whitelist(methods=["POST"])
@idempotent("mes.complete_operation")
def complete_operation(
	job_card: str,
	completed_qty: float,
	employee: str | None = None,
	from_time: str | None = None,
	to_time: str | None = None,
) -> dict[str, Any]:
	"""Log a finished operation on a Job Card (time log + quantity) and submit it when done."""
	frappe.only_for(MES_ROLES)
	jc = get_scoped_doc("Job Card", job_card)
	if jc.docstatus != 0:
		frappe.throw(_("Job Card {0} is already closed.").format(job_card))
	start = from_time or now_datetime()
	end = to_time or now_datetime()
	jc.append(
		"time_logs",
		{"from_time": start, "to_time": end, "completed_qty": flt(completed_qty), "employee": employee},
	)
	jc.flags.ignore_permissions = True
	jc.save()
	if flt(jc.total_completed_qty) >= flt(jc.for_quantity):
		jc.submit()
	return {"job_card": jc.name, "status": jc.status, "completed_qty": flt(jc.total_completed_qty)}
