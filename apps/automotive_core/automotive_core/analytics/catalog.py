"""KPI catalogue sync: every app declares its KPI definitions through the
``automotive_kpi_definitions`` hook; migrate upserts them into KPI Definition."""

from __future__ import annotations

import frappe

from automotive_core.utils import hook_list

FIELDS = (
	"kpi_name",
	"domain",
	"unit",
	"direction",
	"target_value",
	"refresh_policy",
	"compute_method",
	"reconciliation_method",
	"owner_role",
	"formula",
	"source_doctypes",
	"authorization_filter",
	"reconciliation_check",
)


def definitions() -> list[dict]:
	out: list[dict] = []
	for path in hook_list("automotive_kpi_definitions"):
		out.extend(frappe.get_attr(path))
	return out


def sync() -> None:
	for item in definitions():
		values = {k: item.get(k) for k in FIELDS}
		if frappe.db.exists("KPI Definition", item["kpi_code"]):
			doc = frappe.get_doc("KPI Definition", item["kpi_code"])
			if all(doc.get(k) == v for k, v in values.items() if v is not None):
				continue
			doc.update(values)
		else:
			doc = frappe.get_doc({"doctype": "KPI Definition", "kpi_code": item["kpi_code"], "is_active": 1, **values})
		doc.flags.ignore_permissions = True
		doc.save()
