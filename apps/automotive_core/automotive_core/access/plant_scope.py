"""Plant scoping for transactional documents (ERP-01, ERP-10).

DocTypes listed in the ``automotive_plant_scoped_doctypes`` hook carry a
``automotive_plant`` link. When empty it is derived from the document's work order,
workstation, warehouses or references, and (with enforcement on) it is required
whenever the company operates plants. Visibility is then enforced by Frappe's
User Permissions on Plant (list views, reports, API, link queries) because every
scoped DocType links to Plant.
"""

from __future__ import annotations

import frappe
from frappe import _

from automotive_core.utils import hook_set, local_cache

HEADER_WAREHOUSE_FIELDS = (
	"set_warehouse",
	"from_warehouse",
	"to_warehouse",
	"fg_warehouse",
	"wip_warehouse",
	"source_warehouse",
	"set_from_warehouse",
	"set_target_warehouse",
	"parent_warehouse",
)
ROW_WAREHOUSE_FIELDS = ("warehouse", "s_warehouse", "t_warehouse", "source_warehouse", "from_warehouse")
LINKED_SCOPED_FIELDS = ("work_order", "production_plan", "material_request", "purchase_order")


class PlantRequiredError(frappe.ValidationError):
	pass


def _request_cache() -> dict:
	return local_cache("automotive_plant_cache")


def warehouse_plant(warehouse: str | None) -> str | None:
	if not warehouse:
		return None
	cache = _request_cache()
	key = ("Warehouse", warehouse)
	if key not in cache:
		cache[key] = frappe.db.get_value("Warehouse", warehouse, "automotive_plant")
	return cache[key]


def workstation_plant(workstation: str | None) -> str | None:
	if not workstation:
		return None
	cache = _request_cache()
	key = ("Workstation", workstation)
	if key not in cache:
		cache[key] = frappe.db.get_value("Workstation", workstation, "automotive_plant")
	return cache[key]


def company_single_plant(company: str | None) -> str | None:
	if not company:
		return None
	cache = _request_cache()
	key = ("Company", company)
	if key not in cache:
		plants = frappe.get_all("Plant", filters={"company": company, "is_active": 1}, pluck="name", limit=2)
		cache[key] = plants[0] if len(plants) == 1 else None
	return cache[key]


def company_has_plants(company: str | None) -> bool:
	return bool(company) and bool(frappe.db.exists("Plant", {"company": company}))


def derive_plant(doc) -> str | None:
	for field in LINKED_SCOPED_FIELDS:
		value = doc.get(field) if doc.meta.has_field(field) else None
		if value:
			doctype = doc.meta.get_field(field).options
			if frappe.get_meta(doctype).has_field("automotive_plant"):
				plant = frappe.db.get_value(doctype, value, "automotive_plant")
				if plant:
					return plant
	if doc.get("workstation"):
		plant = workstation_plant(doc.workstation)
		if plant:
			return plant
	for field in HEADER_WAREHOUSE_FIELDS:
		plant = warehouse_plant(doc.get(field)) if doc.meta.has_field(field) else None
		if plant:
			return plant
	for row in doc.get("items") or []:
		for field in ROW_WAREHOUSE_FIELDS:
			plant = warehouse_plant(row.get(field))
			if plant:
				return plant
	ref_type, ref_name = doc.get("reference_type"), doc.get("reference_name")
	if ref_type and ref_name and frappe.get_meta(ref_type).has_field("automotive_plant"):
		plant = frappe.db.get_value(ref_type, ref_name, "automotive_plant")
		if plant:
			return plant
	return company_single_plant(doc.get("company"))


def ensure_plant(doc, method=None) -> None:
	if doc.doctype not in hook_set("automotive_plant_scoped_doctypes"):
		return
	if not doc.meta.has_field("automotive_plant"):
		return  # custom fields not yet applied (first migrate)
	if not doc.get("automotive_plant"):
		doc.automotive_plant = derive_plant(doc)
	if doc.automotive_plant and doc.get("company"):
		plant_company = frappe.get_cached_value("Plant", doc.automotive_plant, "company")
		if plant_company and plant_company != doc.company:
			frappe.throw(
				_("Plant {0} belongs to {1}, not {2}.").format(
					doc.automotive_plant, plant_company, doc.company
				)
			)
	if not doc.automotive_plant and _enforced() and company_has_plants(doc.get("company")):
		raise PlantRequiredError(
			_("{0} {1}: set the Plant (it could not be derived from warehouses or the work order).").format(
				_(doc.doctype), doc.name or ""
			)
		)


def _enforced() -> bool:
	cache = _request_cache()
	if "enforce" not in cache:
		cache["enforce"] = bool(frappe.db.get_single_value("Automotive Settings", "enforce_plant_scope"))
	return cache["enforce"]


def get_scoped_doc(doctype: str, name: str, ptype: str = "read", **kwargs):
	"""A document for a whitelisted action that then writes it past the DocType permissions (a
	status change, a recorded step). The caller must be allowed to ``ptype`` it, which applies
	their User Permissions too: they act only on documents of their own plant and company."""
	doc = frappe.get_doc(doctype, name, **kwargs)
	doc.check_permission(ptype)
	return doc
