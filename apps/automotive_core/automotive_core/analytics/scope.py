"""Row-level scope for script reports (ERP-10, ERP-12).

Script reports read ledgers with SQL, which bypasses Frappe's User Permissions, so every
report narrows its query to the companies and plants the viewer may see. A requested
company/plant outside that scope is refused rather than silently emptied.
"""

from __future__ import annotations

import frappe
from frappe import _

from automotive_core.access.user_permissions import scoped_values


def companies(filters: dict | None = None, user: str | None = None) -> list[str]:
	user = user or frappe.session.user
	allowed = scoped_values(user, "Company")
	requested = (filters or {}).get("company")
	if requested:
		if allowed is not None and requested not in allowed:
			raise frappe.PermissionError(_("You have no access to company {0}.").format(requested))
		return [requested]
	return sorted(allowed) if allowed is not None else frappe.get_all("Company", pluck="name", order_by="name")


def plants(filters: dict | None = None, user: str | None = None, company_list: list[str] | None = None) -> list[str]:
	"""Plants in scope; always a concrete list (for SQL ``in``)."""
	user = user or frappe.session.user
	allowed = scoped_values(user, "Plant")
	requested = (filters or {}).get("plant")
	if requested:
		if allowed is not None and requested not in allowed:
			raise frappe.PermissionError(_("You have no access to plant {0}.").format(requested))
		return [requested]
	company_list = company_list if company_list is not None else companies(filters, user)
	in_companies = frappe.get_all("Plant", filters={"company": ("in", company_list or [""])}, pluck="name",
		order_by="name")
	return [p for p in in_companies if allowed is None or p in allowed]


def sql_in(values: list[str]) -> tuple:
	"""A tuple for ``in %(x)s`` that is never empty (an empty scope matches nothing)."""
	return tuple(values) if values else ("",)
