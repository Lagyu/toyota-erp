"""KPI computation service: scoped reads for dashboards and reconciled snapshots (ERP-12).

Every KPI compute function has the signature

    compute(company: str, from_date, to_date, plant: str | None = None) -> KpiResult

and reads only ERPNext/our ledgers and documents. A reconciliation function checks
the result against an independent ledger total and returns (ok, note). Dashboard
reads (``get_kpi``) enforce the caller's company/plant User Permissions; snapshots
store the inputs hash so a value can be reproduced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, getdate, now_datetime, nowdate

from automotive_core.access.user_permissions import scoped_values
from automotive_core.utils import canonical_json, commit, sha256_hex


@dataclass
class KpiResult:
	value: float | None
	numerator: float | None = None
	denominator: float | None = None
	inputs: dict[str, Any] = field(default_factory=dict)

	@property
	def inputs_hash(self) -> str:
		return sha256_hex(canonical_json(self.inputs))


def ratio(numerator: float, denominator: float, scale: float = 100.0) -> float | None:
	return round(numerator / denominator * scale, 4) if denominator else None


def _authorised_scope(company: str, plant: str | None) -> None:
	allowed_companies = scoped_values(frappe.session.user, "Company")
	if allowed_companies is not None and company not in allowed_companies:
		raise frappe.PermissionError(_("You have no access to company {0}.").format(company))
	allowed_plants = scoped_values(frappe.session.user, "Plant")
	if allowed_plants is not None:
		if plant is None:
			raise frappe.PermissionError(_("Select one of your plants."))
		if plant not in allowed_plants:
			raise frappe.PermissionError(_("You have no access to plant {0}.").format(plant))


def compute(kpi_code: str, company: str, from_date, to_date, plant: str | None = None) -> KpiResult:
	definition = frappe.get_cached_doc("KPI Definition", kpi_code)
	fn = frappe.get_attr(definition.compute_method)
	return fn(company=company, from_date=getdate(from_date), to_date=getdate(to_date), plant=plant)


def reconcile(kpi_code: str, company: str, from_date, to_date, plant: str | None, result: KpiResult) -> tuple[bool, str]:
	definition = frappe.get_cached_doc("KPI Definition", kpi_code)
	fn = frappe.get_attr(definition.reconciliation_method)
	return fn(company=company, from_date=getdate(from_date), to_date=getdate(to_date), plant=plant, result=result)


@frappe.whitelist()
def get_kpi(kpi_code: str, company: str, from_date: str | None = None, to_date: str | None = None,
		plant: str | None = None) -> dict[str, Any]:
	frappe.has_permission("KPI Definition", "read", throw=True)
	_authorised_scope(company, plant)
	to_date = getdate(to_date or nowdate())
	from_date = getdate(from_date or add_days(to_date, -29))
	result = compute(kpi_code, company, from_date, to_date, plant)
	definition = frappe.get_cached_doc("KPI Definition", kpi_code)
	return {
		"kpi": kpi_code,
		"name": _(definition.kpi_name),
		"unit": definition.unit,
		"target": definition.target_value,
		"direction": definition.direction,
		"value": result.value,
		"numerator": result.numerator,
		"denominator": result.denominator,
		"from_date": str(from_date),
		"to_date": str(to_date),
		"company": company,
		"plant": plant,
		"inputs_hash": result.inputs_hash,
	}


def snapshot(kpi_code: str, company: str, from_date, to_date, plant: str | None = None) -> str:
	result = compute(kpi_code, company, from_date, to_date, plant)
	ok, note = reconcile(kpi_code, company, from_date, to_date, plant, result)
	doc = frappe.get_doc(
		{
			"doctype": "KPI Snapshot",
			"kpi": kpi_code,
			"company": company,
			"plant": plant,
			"period_start": from_date,
			"period_end": to_date,
			"value": result.value,
			"numerator": result.numerator,
			"denominator": result.denominator,
			"computed_at": now_datetime(),
			"inputs_hash": result.inputs_hash,
			"reconciliation_status": "Passed" if ok else "Failed",
			"reconciliation_note": note[:1000],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	return doc.name


def refresh_daily() -> None:
	"""Snapshot every Daily KPI for each company for the trailing 30 days."""
	to_date = add_days(getdate(nowdate()), -1)
	from_date = add_days(to_date, -29)
	companies = frappe.get_all("Company", pluck="name")
	for kpi in frappe.get_all("KPI Definition", filters={"is_active": 1, "refresh_policy": "Daily"}, pluck="name"):
		for company in companies:
			try:
				snapshot(kpi, company, from_date, to_date)
			except ImportError:
				frappe.log_error(title=f"KPI {kpi}: compute method not importable")
	commit()
