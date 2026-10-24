"""Quality Inspection outcomes drive holds, nonconformances and vehicle release (ERP-07).

* Rejected inspection (incoming, in-process or final): a Quality Hold on the inspected
  batch/serial (or item) plus a draft Non Conformance linked to both.
* Accepted final / pre-delivery inspection of a vehicle: quality status *Released*,
  unless an active hold still covers the unit.
Inspections are submittable and therefore immutable; a correction is a cancel (with
reason, audited) and an amended inspection. After rework a unit is inspected again with a
new inspection of the same document row (``make_reinspection``); the rejected one stays.
"""

from __future__ import annotations

import frappe
from frappe import _

from automotive_core.governance import audit
from automotive_manufacturing.quality_control.holds import active_holds_for, raise_hold


def _company(doc) -> str | None:
	if doc.get("company"):
		return doc.company
	if doc.reference_type and doc.reference_name and frappe.get_meta(doc.reference_type).has_field("company"):
		return frappe.db.get_value(doc.reference_type, doc.reference_name, "company")
	return frappe.defaults.get_defaults().get("company")


def on_submit(doc, method=None) -> None:
	audit.record("quality.inspection_submitted", doc.doctype, doc.name,
		{"status": doc.status, "stage": doc.get("automotive_inspection_stage"), "item": doc.item_code,
		 "batch": doc.get("batch_no"), "serial": doc.get("item_serial_no"), "reference": [doc.reference_type,
		 doc.reference_name]}, company=_company(doc))
	if doc.status == "Rejected":
		_on_rejected(doc)
	elif doc.status == "Accepted" and doc.get("item_serial_no") and doc.get("automotive_inspection_stage") in (
		"Final", "Pre-Delivery"
	):
		serial = doc.item_serial_no
		if frappe.db.exists("Vehicle Unit", serial):
			held = active_holds_for(item=doc.item_code, serials=[serial])
			frappe.db.set_value("Vehicle Unit", serial, {
				"quality_status": "On Hold" if held else "Released",
				"final_inspection": doc.name,
			})


def _on_rejected(doc) -> None:
	company = _company(doc)
	serial, batch = doc.get("item_serial_no"), doc.get("batch_no")
	if serial:
		scope, targets = "Serial No", {"serial_no": serial, "item": doc.item_code}
	elif batch:
		scope, targets = "Batch", {"batch_no": batch, "item": doc.item_code}
	else:
		scope, targets = "Item", {"item": doc.item_code}
	plant = None
	if doc.reference_type and doc.reference_name and frappe.get_meta(doc.reference_type).has_field("automotive_plant"):
		plant = frappe.db.get_value(doc.reference_type, doc.reference_name, "automotive_plant")
	hold = raise_hold(scope, _("Rejected inspection {0}").format(doc.name), company, source=doc, plant=plant,
		**targets)
	supplier = None
	if doc.reference_type == "Purchase Receipt" and doc.reference_name:
		supplier = frappe.db.get_value("Purchase Receipt", doc.reference_name, "supplier")
	nc = frappe.get_doc(
		{
			"doctype": "Non Conformance",
			"subject": _("{0} rejected at {1} inspection").format(doc.item_code, doc.get("automotive_inspection_stage") or _(doc.inspection_type)),
			"procedure": _ensure_procedure(),
			"process_owner": _owner(),
			"status": "Open",
			"details": doc.remarks or _("Readings outside acceptance criteria."),
			"automotive_source": {"Incoming": "Incoming", "In-Process": "In-Process", "Final": "Final",
				"Pre-Delivery": "Final"}.get(doc.get("automotive_inspection_stage"), "In-Process"),
			"automotive_company": company,
			"automotive_plant": plant,
			"automotive_item": doc.item_code,
			"automotive_batch_no": batch,
			"automotive_serial_no": serial,
			"automotive_qty_affected": doc.sample_size,
			"automotive_supplier": supplier,
			"automotive_quality_inspection": doc.name,
			"automotive_quality_hold": hold,
		}
	)
	nc.flags.ignore_permissions = True
	nc.insert()
	doc.db_set({"automotive_quality_hold": hold, "automotive_nonconformance": nc.name})


def _ensure_procedure() -> str:
	name = "Nonconforming Material Control"
	if not frappe.db.exists("Quality Procedure", name):
		frappe.get_doc({"doctype": "Quality Procedure", "quality_procedure_name": name}).insert(ignore_permissions=True)
	return frappe.db.get_value("Quality Procedure", {"quality_procedure_name": name}, "name")


def _owner() -> str:
	return frappe.session.user if frappe.session.user != "Guest" else "Administrator"


def on_cancel(doc, method=None) -> None:
	serial = doc.get("item_serial_no")
	if serial and frappe.db.get_value("Vehicle Unit", serial, "final_inspection") == doc.name:
		frappe.db.set_value("Vehicle Unit", serial, {"quality_status": "Pending Inspection", "final_inspection": None})


@frappe.whitelist(methods=["POST"])
def make_reinspection(quality_inspection: str, status: str = "Accepted", remarks: str | None = None) -> str:
	"""Re-inspect a unit/lot after rework: a new inspection of the same document row as the
	rejected one (ERPNext allows one inspection per row unless the row is named explicitly).
	Returns the draft inspection; the inspector submits it."""
	frappe.has_permission("Quality Inspection", "create", throw=True)
	original = frappe.get_doc("Quality Inspection", quality_inspection)
	if original.docstatus != 1 or original.status != "Rejected":
		frappe.throw(_("Only a submitted, rejected inspection can be re-inspected ({0} is {1}).").format(
			original.name, original.status))
	doc = frappe.get_doc({
		"doctype": "Quality Inspection",
		"inspection_type": original.inspection_type,
		"automotive_inspection_stage": original.get("automotive_inspection_stage"),
		"reference_type": original.reference_type,
		"reference_name": original.reference_name,
		"child_row_reference": original.child_row_reference,
		"item_code": original.item_code,
		"item_serial_no": original.item_serial_no,
		"batch_no": original.batch_no,
		"sample_size": original.sample_size,
		"company": original.company,
		"quality_inspection_template": original.quality_inspection_template,
		"inspected_by": frappe.session.user,
		"manual_inspection": 1,
		"status": status,
		"remarks": remarks or _("Re-inspection after rework of {0}").format(original.name),
	})
	doc.insert()
	return doc.name
