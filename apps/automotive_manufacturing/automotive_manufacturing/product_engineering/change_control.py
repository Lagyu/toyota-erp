"""Engineering change control: ECR impact assessment and ECO implementation (ERP-02, J-04).

ECO implementation runs on submit (after maker/checker approval enforced by the
approval engine) inside ``automotive_eco_context``:

1. For every target BOM, copy it, apply the listed changes, and release the copy as
   the next revision effective from the ECO date (superseding the old revision,
   whose effective window is closed the day before).
2. Ripple up: parent BOMs that explicitly reference a superseded sub-assembly BOM
   get their own new revision pointing at the new one, recursively.
3. Submitted Work Orders are never touched: they stay on their approved revision.
   Draft orders planned on/after the effective date pick up the new revision when
   next saved (bom_revisions.select_effective_bom).
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, now_datetime, nowdate

from automotive_manufacturing.product_engineering.bom_revisions import next_revision

OPEN_WO_STATUSES = ("Not Started", "In Process", "Material Transferred", "Draft")


# ---------------------------------------------------------------------------------------
# Impact assessment
# ---------------------------------------------------------------------------------------


def assess(doc) -> dict:
	items = [row.item_code for row in doc.affected_items]
	boms = [row.current_bom for row in doc.affected_items if row.current_bom]
	parents = set()
	for item in items:
		parents |= {r["bom"] for r in where_used(item)}
	all_boms = sorted(set(boms) | parents)
	open_wos = frappe.get_all(
		"Work Order",
		filters={"docstatus": ("<", 2), "status": ("in", OPEN_WO_STATUSES), "bom_no": ("in", all_boms or [""])},
		fields=["name", "qty", "produced_qty"],
	)
	on_hand = flt(frappe.db.sql("select sum(actual_qty) from `tabBin` where item_code in %s", [tuple(items) or ("",)])[0][0])
	open_po = flt(
		frappe.db.sql(
			"""select sum(poi.stock_qty - poi.received_qty * poi.conversion_factor) from `tabPurchase Order Item` poi
			join `tabPurchase Order` po on po.name = poi.parent
			where po.docstatus = 1 and po.status not in ('Closed', 'Completed') and poi.item_code in %s""",
			[tuple(items) or ("",)],
		)[0][0]
	)
	wip = sum(flt(w.qty) - flt(w.produced_qty) for w in open_wos)
	summary = _(
		"{0} affected item(s); used in {1} BOM(s); {2} open work order(s) ({3} units unbuilt); "
		"{4} on hand; {5} on open purchase orders."
	).format(len(items), len(all_boms), len(open_wos), wip, on_hand, open_po)
	return {
		"open_work_orders": len(open_wos),
		"wip_qty": wip,
		"on_hand_qty": on_hand,
		"open_purchase_qty": open_po,
		"impact_summary": summary,
		"impact_assessed_at": now_datetime(),
	}


@frappe.whitelist(methods=["POST"])
def assess_impact(name: str) -> dict:
	doc = frappe.get_doc("Engineering Change Request", name)
	doc.check_permission("write" if doc.docstatus == 0 else "read")
	result = assess(doc)
	if doc.docstatus == 0:
		doc.update(result)
		doc.save()
	else:
		doc.db_set(result)
	return result


@frappe.whitelist()
def make_eco(source_name: str | None = None, ecr: str | None = None):
	"""Mapper (Create > ECO on a submitted change request): an unsaved ECO with one quantity
	change per affected BOM line, prefilled with today's quantity for the engineer to set."""
	if not (source_name or ecr):
		frappe.throw(_("Choose the change request to convert."))
	request = frappe.get_doc("Engineering Change Request", source_name or ecr)
	request.check_permission("read")
	if request.docstatus != 1 or request.status not in ("Under Review", "Approved"):
		frappe.throw(_("Only submitted change requests can be converted."))
	eco = frappe.new_doc("Engineering Change Order")
	eco.title = request.title
	eco.engineering_change_request = request.name
	eco.vehicle_model = request.vehicle_model
	eco.effective_date = add_days(nowdate(), 7)
	eco.stock_disposition = "Use Existing Stock"
	for row in request.affected_items:
		if not row.current_bom:
			continue
		eco.company = eco.company or frappe.db.get_value("BOM", row.current_bom, "company")
		eco.append("changes", {"change_type": "Change Quantity", "target_bom": row.current_bom,
			"component": row.item_code, "reason": row.proposed_change,
			"qty": frappe.db.get_value("BOM Item", {"parent": row.current_bom, "item_code": row.item_code}, "qty")})
	if not eco.changes:
		frappe.throw(_("Name the current BOM of at least one affected item first."))
	return eco


# ---------------------------------------------------------------------------------------
# Where used
# ---------------------------------------------------------------------------------------


def where_used(item_code: str, released_only: bool = True, max_depth: int = 10) -> list[dict]:
	"""Every BOM (any level) that consumes ``item_code``; rows carry the path."""
	result, frontier, seen = [], [(item_code, item_code)], set()
	for depth in range(1, max_depth + 1):
		next_frontier = []
		for code, path in frontier:
			filters = "and b.docstatus = 1 and b.is_active = 1" if released_only else "and b.docstatus < 2"
			rows = frappe.db.sql(
				f"""select distinct b.name, b.item, bi.qty from `tabBOM Item` bi join `tabBOM` b on b.name = bi.parent
				where bi.item_code = %s {filters}""",  # noqa: S608 (constant SQL fragments; values are bound)
				code,
				as_dict=True,
			)
			for row in rows:
				if row.name in seen:
					continue
				seen.add(row.name)
				result.append({"bom": row.name, "parent_item": row.item, "qty": row.qty, "level": depth,
					"path": f"{path} -> {row.item}"})
				next_frontier.append((row.item, f"{path} -> {row.item}"))
		if not next_frontier:
			break
		frontier = next_frontier
	return result


@frappe.whitelist()
def get_where_used(item_code: str) -> list[dict]:
	frappe.has_permission("BOM", "read", throw=True)
	return where_used(item_code)


# ---------------------------------------------------------------------------------------
# ECO
# ---------------------------------------------------------------------------------------


def validate_eco(doc) -> None:
	for row in doc.changes:
		bom = frappe.db.get_value("BOM", row.target_bom, ["docstatus", "is_active", "automotive_superseded_by", "item"],
			as_dict=True)
		if not bom or bom.docstatus != 1 or not bom.is_active:
			frappe.throw(_("Row {0}: BOM {1} is not an active released BOM.").format(row.idx, row.target_bom))
		if bom.automotive_superseded_by:
			frappe.throw(_("Row {0}: BOM {1} is already superseded by {2}.").format(row.idx, row.target_bom,
				bom.automotive_superseded_by))
		components = set(frappe.get_all("BOM Item", filters={"parent": row.target_bom}, pluck="item_code"))
		if row.change_type in ("Remove Component", "Replace Component", "Change Quantity") and row.component not in components:
			frappe.throw(_("Row {0}: {1} is not in BOM {2}.").format(row.idx, row.component, row.target_bom))
		if row.change_type in ("Add Component", "Replace Component") and not row.new_component and row.change_type != "Add Component":
			frappe.throw(_("Row {0}: choose the new component.").format(row.idx))
		if row.change_type == "Add Component" and not (row.new_component or row.component):
			frappe.throw(_("Row {0}: choose the component to add.").format(row.idx))
		if row.change_type in ("Add Component", "Change Quantity") and flt(row.qty) <= 0:
			frappe.throw(_("Row {0}: quantity must be positive.").format(row.idx))
		if row.change_type == "Change Quantity":
			current = frappe.db.get_value("BOM Item", {"parent": row.target_bom, "item_code": row.component}, "qty")
			if abs(flt(current) - flt(row.qty)) < 1e-9:
				frappe.throw(_("Row {0}: BOM {1} already uses {2} of {3}; enter the new quantity.").format(
					row.idx, row.target_bom, flt(current), row.component))
	if doc.is_new() and getdate(doc.effective_date) < getdate(nowdate()):
		frappe.throw(_("An engineering change cannot take effect in the past."))


def _apply_changes(new_bom, changes) -> None:
	for row in changes:
		if row.change_type == "Remove Component":
			new_bom.items = [i for i in new_bom.items if i.item_code != row.component]
		elif row.change_type == "Replace Component":
			for item in new_bom.items:
				if item.item_code == row.component:
					item.item_code = row.new_component
					item.item_name = frappe.get_cached_value("Item", row.new_component, "item_name")
					item.bom_no = frappe.get_cached_value("Item", row.new_component, "default_bom")
					if flt(row.qty):
						item.qty = flt(row.qty)
						item.automotive_net_qty = flt(row.qty)
					item.automotive_scrap_factor_percent = flt(row.scrap_factor_percent) or item.automotive_scrap_factor_percent
		elif row.change_type == "Change Quantity":
			for item in new_bom.items:
				if item.item_code == row.component:
					item.qty = flt(row.qty)
					item.automotive_net_qty = flt(row.qty)
					if flt(row.scrap_factor_percent):
						item.automotive_scrap_factor_percent = flt(row.scrap_factor_percent)
		elif row.change_type == "Add Component":
			code = row.new_component or row.component
			new_bom.append("items", {
				"item_code": code,
				"qty": flt(row.qty),
				"automotive_net_qty": flt(row.qty),
				"automotive_scrap_factor_percent": flt(row.scrap_factor_percent),
				"uom": frappe.get_cached_value("Item", code, "stock_uom"),
				"bom_no": frappe.get_cached_value("Item", code, "default_bom"),
			})
	if not new_bom.items:
		frappe.throw(_("The change would leave BOM {0} without components.").format(new_bom.automotive_supersedes))


def _release_revision(old_name: str, eco, changes, reason: str, replace_bom: dict[str, str] | None = None) -> str:
	old = frappe.get_doc("BOM", old_name)
	new = frappe.copy_doc(old)
	new.automotive_supersedes = old.name
	new.automotive_superseded_by = None
	new.automotive_revision = next_revision([r for r in frappe.get_all("BOM", filters={"item": old.item,
		"docstatus": ("<", 2)}, pluck="automotive_revision")])
	new.automotive_effective_from = eco.effective_date
	new.automotive_effective_to = None
	new.automotive_eco = eco.name
	new.automotive_engineering_status = "Draft"
	new.is_default = 1 if getdate(eco.effective_date) <= getdate(nowdate()) else 0
	if changes:
		_apply_changes(new, changes)
	for row in new.items:
		if replace_bom and row.bom_no in replace_bom:
			row.bom_no = replace_bom[row.bom_no]
	new.flags.ignore_permissions = True
	new.insert()
	new.submit()
	effective_to = add_days(getdate(eco.effective_date), -1)
	old.db_set({
		"automotive_effective_to": effective_to,
		"automotive_superseded_by": new.name,
		"automotive_engineering_status": "Superseded" if getdate(eco.effective_date) <= getdate(nowdate()) else "Released",
	})
	eco.append("revisions", {"old_bom": old.name, "new_bom": new.name, "revision": new.automotive_revision,
		"reason": reason})
	return new.name


def implement(eco) -> None:
	frappe.flags.automotive_eco_context = True
	try:
		by_bom: dict[str, list] = defaultdict(list)
		for row in eco.changes:
			by_bom[row.target_bom].append(row)
		replaced: dict[str, str] = {}
		for bom_name in sorted(by_bom):
			replaced[bom_name] = _release_revision(bom_name, eco, by_bom[bom_name], "Direct Change")
		# ripple: parents explicitly referencing a superseded BOM get a new revision
		frontier = dict(replaced)
		while frontier:
			parents = frappe.db.sql(
				"""select distinct bi.parent from `tabBOM Item` bi join `tabBOM` b on b.name = bi.parent
				where bi.bom_no in %s and b.docstatus = 1 and b.is_active = 1
				and ifnull(b.automotive_superseded_by, '') = ''""",
				[tuple(frontier)],
			)
			next_frontier = {}
			for (parent,) in parents:
				if parent in replaced:
					continue
				new_parent = _release_revision(parent, eco, [], "Parent Of Changed Assembly", replace_bom=replaced)
				replaced[parent] = new_parent
				next_frontier[parent] = new_parent
			frontier = next_frontier
		eco.db_set({"implementation_status": "Implemented", "implemented_at": now_datetime()})
		for row in eco.revisions:
			row.db_insert() if row.is_new() else row.db_update()
		if eco.engineering_change_request:
			frappe.db.set_value("Engineering Change Request", eco.engineering_change_request, "status", "Converted")
	finally:
		frappe.flags.automotive_eco_context = False


def revert(eco) -> None:
	"""ECO cancellation: allowed only while no Work Order used any new revision."""
	new_boms = [r.new_bom for r in eco.revisions]
	used = frappe.get_all("Work Order", filters={"bom_no": ("in", new_boms or [""]), "docstatus": 1}, pluck="name", limit=1)
	if used:
		frappe.throw(_("Revision used by Work Order {0}; issue a new ECO instead of cancelling.").format(used[0]))
	frappe.flags.automotive_eco_context = True
	try:
		for row in reversed(eco.revisions):
			new = frappe.get_doc("BOM", row.new_bom)
			if new.docstatus == 1:
				new.flags.ignore_permissions = True
				new.cancel()
			frappe.db.set_value("BOM", row.old_bom, {"automotive_effective_to": None, "automotive_superseded_by": None,
				"automotive_engineering_status": "Released", "is_active": 1})
	finally:
		frappe.flags.automotive_eco_context = False
	eco.db_set("implementation_status", "Not Started")
