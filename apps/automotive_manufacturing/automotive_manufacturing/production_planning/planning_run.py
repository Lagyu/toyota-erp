"""Planning Run orchestration: queued background job with progress, cancellation and
durable status; replay from the stored snapshot; release through ERPNext's native
Production Plan (Work Orders) and Material Requests (ERP-03, J-03).
"""

from __future__ import annotations

import json
import traceback

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, flt, getdate, now_datetime, nowdate

from automotive_core.access.plant_scope import get_scoped_doc
from automotive_core.governance import audit
from automotive_core.utils import canonical_json, commit, sha256_hex
from automotive_manufacturing.engine import mrp
from automotive_manufacturing.production_planning import snapshot as snapshot_mod

PLANNER_ROLES = ("Production Planner", "Plant Director", "System Manager")


class PlanningCancelled(Exception):
	pass


def outputs_hash(result: dict) -> str:
	return sha256_hex(canonical_json(result))


@frappe.whitelist(methods=["POST"])
def start(company: str, plant: str, parameter_set: str, master_production_schedule: str | None = None) -> str:
	frappe.only_for(PLANNER_ROLES)
	params = get_scoped_doc("Planning Parameter Set", parameter_set)
	if params.docstatus != 1:
		frappe.throw(_("Only submitted (approved) parameter versions can be used."))
	if params.plant != plant or params.company != company:
		frappe.throw(_("Parameter set {0} is for {1} / {2}.").format(parameter_set, params.company, params.plant))
	run = frappe.get_doc({"doctype": "Planning Run", "company": company, "plant": plant, "parameter_set": parameter_set,
		"master_production_schedule": master_production_schedule, "status": "Queued"})
	run.insert()
	enqueue(run.name)
	return run.name


def enqueue(name: str) -> None:
	if frappe.flags.in_test or frappe.flags.automotive_inline_planning:
		execute(name)
		return
	frappe.enqueue("automotive_manufacturing.production_planning.planning_run.execute", queue="long", timeout=3600,
		job_id=f"planning-run-{name}", deduplicate=True, enqueue_after_commit=True, name=name)


def _progress(run, percent: int) -> None:
	run.db_set("progress", percent, update_modified=False)
	commit()
	if cint(frappe.db.get_value("Planning Run", run.name, "cancel_requested")):
		raise PlanningCancelled


def execute(name: str) -> str:
	run = frappe.get_doc("Planning Run", name)
	if run.status not in ("Queued", "Failed"):
		return run.status
	run.db_set({"status": "Running", "progress": 1, "as_of": run.as_of or now_datetime(), "error": None})
	commit()
	try:
		params = frappe.get_doc("Planning Parameter Set", run.parameter_set)
		if run.replay_of:
			snap = load_snapshot(run.replay_of)
		else:
			snap = snapshot_mod.collect(run, params)
		_progress(run, 40)
		raw = canonical_json(snap)
		inputs_sha = sha256_hex(raw)
		file_url = _store_snapshot(run.name, raw)
		result = mrp.plan(snap)
		_progress(run, 80)
		run.reload()
		run.set("proposals", [{**p, "decision": "Proposed"} for p in result["proposals"]])
		run.set("shortages", result["shortages"])
		run.set("capacity", result["capacity"])
		run.update({
			"inputs_file": file_url,
			"inputs_sha256": inputs_sha,
			"outputs_sha256": outputs_hash(result),
			"horizon_start": snap["as_of"],
			"horizon_end": frappe.utils.add_days(snap["as_of"], snap["parameters"]["horizon_days"]),
			"demand_lines": len(snap["demand"]),
			"proposal_count": len(result["proposals"]),
			"shortage_count": len(result["shortages"]),
			"capacity_conflicts": sum(c["conflict"] for c in result["capacity"]),
			"status": "Completed",
			"progress": 100,
		})
		if run.replay_of:
			original = frappe.db.get_value("Planning Run", run.replay_of, "outputs_sha256")
			run.replay_result = "Identical" if original == run.outputs_sha256 else "Different"
		run.flags.ignore_permissions = True
		run.save()
		commit()
	except PlanningCancelled:
		frappe.db.rollback()
		frappe.db.set_value("Planning Run", name, {"status": "Cancelled", "progress": 0})
		commit()
	except Exception:
		frappe.db.rollback()
		frappe.db.set_value("Planning Run", name, {"status": "Failed", "error": traceback.format_exc()[-4000:]})
		commit()
		if frappe.flags.in_test:
			raise
	return frappe.db.get_value("Planning Run", name, "status")


def _store_snapshot(run_name: str, raw: str) -> str:
	from frappe.utils.file_manager import save_file

	saved = save_file(f"{run_name}-inputs.json", raw.encode(), "Planning Run", run_name, is_private=1)
	return saved.file_url


def load_snapshot(run_name: str) -> dict:
	url = frappe.db.get_value("Planning Run", run_name, "inputs_file")
	if not url:
		frappe.throw(_("Planning Run {0} has no stored inputs.").format(run_name))
	file_doc = frappe.get_doc("File", {"file_url": url})
	content = file_doc.get_content()
	stored_hash = frappe.db.get_value("Planning Run", run_name, "inputs_sha256")
	text = content.decode() if isinstance(content, bytes) else content
	if sha256_hex(text) != stored_hash:
		frappe.throw(_("Stored inputs of {0} do not match their recorded hash.").format(run_name))
	return json.loads(text)


@frappe.whitelist(methods=["POST"])
def replay(name: str) -> str:
	frappe.only_for((*PLANNER_ROLES, "Auditor"))
	original = get_scoped_doc("Planning Run", name)
	run = frappe.get_doc({"doctype": "Planning Run", "company": original.company, "plant": original.plant,
		"parameter_set": original.parameter_set, "replay_of": original.name, "status": "Queued",
		"as_of": original.as_of})
	run.insert(ignore_permissions=True)
	enqueue(run.name)
	return run.name


@frappe.whitelist(methods=["POST"])
def request_cancel(name: str) -> None:
	frappe.only_for(PLANNER_ROLES)
	frappe.db.set_value("Planning Run", name, "cancel_requested", 1)


def recover_stalled() -> None:
	"""Hourly: runs left Running by a dead worker for > 2 hours become Failed (safe to retry)."""
	for name in frappe.get_all("Planning Run", filters={"status": "Running",
			"modified": ("<", add_to_date(now_datetime(), hours=-2))}, pluck="name"):
		frappe.db.set_value("Planning Run", name, {"status": "Failed", "error": "worker stopped; retry the run"})
	commit()


# ---------------------------------------------------------------------------------------
# Release
# ---------------------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def release(name: str) -> dict:
	frappe.only_for(PLANNER_ROLES)
	run = get_scoped_doc("Planning Run", name, for_update=True)
	if run.status != "Completed":
		frappe.throw(_("Only completed runs can be released (status {0}).").format(run.status))
	if run.replay_of:
		frappe.throw(_("A replay is for verification; release the original run."))
	approved = [p for p in run.proposals if p.decision == "Approved"]
	if not approved:
		frappe.throw(_("Approve at least one proposal first."))
	plant = frappe.get_cached_doc("Plant", run.plant)
	manufacture = [p for p in approved if p.proposal_type == "Manufacture"]
	purchase = [p for p in approved if p.proposal_type == "Purchase"]
	created: dict[str, list[str]] = {"Work Order": [], "Material Request": []}
	if manufacture:
		top = min(p.bom_level for p in manufacture)
		pp = frappe.new_doc("Production Plan")
		pp.company = run.company
		pp.posting_date = nowdate()
		pp.for_warehouse = plant.raw_material_warehouse
		for p in manufacture:
			if p.bom_level == top:
				pp.append("po_items", {"item_code": p.item_code, "bom_no": p.bom_no, "planned_qty": p.qty,
					"planned_start_date": p.release_date, "warehouse": plant.finished_goods_warehouse,
					"stock_uom": p.uom, "item_reference": p.name})
		for p in manufacture:
			if p.bom_level != top:
				pp.append("sub_assembly_items", {"production_item": p.item_code, "bom_no": p.bom_no, "qty": p.qty,
					"schedule_date": p.release_date, "fg_warehouse": plant.wip_warehouse or plant.finished_goods_warehouse,
					"type_of_manufacturing": "In House", "stock_uom": p.uom, "uom": p.uom, "bom_level": p.bom_level})
		for p in purchase:
			pp.append("mr_items", {"item_code": p.item_code, "quantity": p.qty, "warehouse": p.warehouse,
				"material_request_type": "Purchase", "schedule_date": p.need_date, "uom": p.uom})
		pp.flags.ignore_permissions = True
		pp.insert()
		pp.submit()
		pp.make_work_order()
		if purchase:
			# approved purchase proposals reach purchasing as submitted requests, as on the
			# purchase-only path; work orders stay planned until the sequence releases them
			pp.submit_material_request = 1
			pp.make_material_request()
		run.production_plan = pp.name
		created["Work Order"] = _post_process_work_orders(pp.name, run, plant)
		created["Material Request"] = frappe.get_all("Material Request Item", filters={"production_plan": pp.name},
			pluck="parent", distinct=True)
	elif purchase:
		created["Material Request"] = [_material_request(run, purchase)]
	_link_proposals(run, created)
	run.status = "Released"
	run.released_at = now_datetime()
	run.released_by = frappe.session.user
	run.flags.ignore_permissions = True
	run.save()
	audit.record("planning.released", run.doctype, run.name,
		{"production_plan": run.production_plan, "work_orders": len(created["Work Order"]),
		 "material_requests": len(created["Material Request"]), "inputs": run.inputs_sha256,
		 "outputs": run.outputs_sha256}, company=run.company)
	return created


def _post_process_work_orders(production_plan: str, run, plant) -> list[str]:
	"""Native Production Plan uses company-wide default warehouses; put the plant's on.

	Finished items go to the plant's FG warehouse; sub-assemblies are produced into the
	line-side warehouse of the line that consumes them, and every order backflushes
	its components from its own line side (skip_transfer)."""
	names = frappe.get_all("Work Order", filters={"production_plan": production_plan}, pluck="name", order_by="name asc")
	manufacture = [p for p in run.proposals if p.proposal_type == "Manufacture" and p.decision == "Approved"]
	top_level = min((p.bom_level for p in manufacture), default=0)
	top_items = {p.item_code for p in manufacture if p.bom_level == top_level}
	top_lines = sorted(filter(None, (snapshot_mod._line_for(code, plant.name) for code in top_items)))
	consuming_line_side = (
		frappe.get_cached_value("Production Line", top_lines[0], "line_side_warehouse") if top_lines else None
	)
	for name in names:
		wo = frappe.get_doc("Work Order", name)
		line = snapshot_mod._line_for(wo.production_item, plant.name)
		line_side = frappe.get_cached_value("Production Line", line, "line_side_warehouse") if line else None
		source = line_side or consuming_line_side or plant.raw_material_warehouse
		is_top = wo.production_item in top_items
		wo.update({
			"automotive_plant": plant.name,
			"automotive_production_line": line or (top_lines[0] if top_lines and not is_top else None),
			"automotive_planning_run": run.name,
			"source_warehouse": source,
			"wip_warehouse": plant.wip_warehouse,
			"fg_warehouse": plant.finished_goods_warehouse if is_top else (consuming_line_side or plant.wip_warehouse),
			"skip_transfer": 1,
		})
		for row in wo.required_items:
			row.source_warehouse = source
		wo.flags.ignore_permissions = True
		wo.save()
	return names


def _material_request(run, purchase) -> str:
	mr = frappe.new_doc("Material Request")
	mr.material_request_type = "Purchase"
	mr.company = run.company
	mr.schedule_date = min(getdate(p.need_date) for p in purchase)
	for p in purchase:
		mr.append("items", {"item_code": p.item_code, "qty": p.qty, "schedule_date": p.need_date,
			"warehouse": p.warehouse, "uom": p.uom})
	mr.flags.ignore_permissions = True
	mr.insert()
	mr.submit()
	return mr.name


def _link_proposals(run, created: dict[str, list[str]]) -> None:
	wo_by_item: dict[str, list[str]] = {}
	for name in created["Work Order"]:
		wo_by_item.setdefault(frappe.db.get_value("Work Order", name, "production_item"), []).append(name)
	mr_by_item: dict[str, str] = {}
	for name in created["Material Request"]:
		for item in frappe.get_all("Material Request Item", filters={"parent": name}, pluck="item_code"):
			mr_by_item.setdefault(item, name)
	for p in run.proposals:
		if p.decision != "Approved":
			continue
		if p.proposal_type == "Manufacture" and wo_by_item.get(p.item_code):
			p.document_type, p.document_name = "Work Order", wo_by_item[p.item_code].pop(0)
		elif p.proposal_type == "Purchase" and p.item_code in mr_by_item:
			p.document_type, p.document_name = "Material Request", mr_by_item[p.item_code]


@frappe.whitelist(methods=["POST"])
def decide_all(name: str, decision: str, proposal_type: str | None = None) -> int:
	"""Planner convenience: approve/reject every proposal (optionally of one type)."""
	frappe.only_for(PLANNER_ROLES)
	if decision not in ("Approved", "Rejected", "Proposed"):
		frappe.throw(_("Invalid decision."))
	run = get_scoped_doc("Planning Run", name)
	if run.status != "Completed":
		frappe.throw(_("Run {0} is {1}.").format(name, run.status))
	count = 0
	for p in run.proposals:
		if not proposal_type or p.proposal_type == proposal_type:
			p.decision = decision
			count += 1
	run.save()
	return count
