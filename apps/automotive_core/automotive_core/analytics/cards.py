"""Workspace number cards backed by the KPI service (ERP-12).

Cards call ``kpi_card`` with ``{"kpi_code": ...}``. The value is computed live for the
viewer's own scope (their default company and, for plant-scoped users, their default
plant) over the trailing 30 days, through ``analytics.kpi.get_kpi`` — the same
authorization and formula as the KPI API, never a stored or fabricated number.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, nowdate

from automotive_core.access.user_permissions import scoped_values
from automotive_core.analytics.kpi import get_kpi
from automotive_core.utils import json_argument

FIELDTYPE = {"Percent": "Percent", "Days": "Float", "Hours": "Float", "Count": "Int", "Amount": "Currency"}


def viewer_scope(user: str | None = None) -> tuple[str | None, str | None]:
	"""(company, plant) a dashboard shows by default for the user."""
	user = user or frappe.session.user
	companies = scoped_values(user, "Company")
	company = frappe.defaults.get_user_default("Company", user=user)
	if companies is not None and company not in companies:
		company = companies[0] if companies else None
	company = company or frappe.db.get_single_value("Global Defaults", "default_company")
	plants = scoped_values(user, "Plant")
	plant = None
	if plants is not None:
		plant = frappe.defaults.get_user_default("Plant", user=user)
		if plant not in plants:
			plant = plants[0] if plants else None
	return company, plant


@frappe.whitelist()
def kpi_card(filters: str | dict | None = None) -> dict:
	filters = frappe._dict(json_argument(filters, "filters", dict) or {})
	kpi_code = filters.get("kpi_code")
	company, plant = viewer_scope()
	company = filters.get("company") or company
	plant = filters.get("plant") or plant
	if not (kpi_code and company):
		return {"value": None}
	to_date = nowdate()
	result = get_kpi(kpi_code, company, from_date=add_days(to_date, -29), to_date=to_date, plant=plant)
	out = {"value": result["value"], "fieldtype": FIELDTYPE.get(result["unit"], "Float")}
	if out["fieldtype"] == "Currency":
		out["options"] = frappe.get_cached_value("Company", company, "default_currency")
	out["route"] = ["Form", "KPI Definition", kpi_code]
	return out
