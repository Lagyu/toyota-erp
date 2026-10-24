"""Tax determination (ENT-7 configurable taxes, ERP-08.4, ERP-09.2).

A party's Tax Category picks each document's tax template through ERPNext Tax Rules, per
company and tax type. The tenant definition (``taxes`` in setup/tenants/*.yaml) creates the
categories, the templates that charge no tax (exports, resale, imports) and the rules, and
turns on Accounts Settings > "Automatically add taxes from Taxes and Charges Template", so
documents created by code get the template's rows as well as those entered in the desk.

New dealers, suppliers and intercompany parties get their category from the defaults table
in Automotive Settings (company country x party country) unless their registration names
one. The rates are ERPNext's country defaults; jurisdiction-specific compliance has to be
validated separately (requirements §2).
"""

from __future__ import annotations

import frappe


def default_tax_category(
	party_type: str, company: str | None = None, country: str | None = None
) -> str | None:
	"""The most specific Automotive Settings row for this party type, company and party
	country: a row naming the party's country beats one for "any other country", and a row
	naming the company's country beats one for any company."""
	company_country = frappe.get_cached_value("Company", company, "country") if company else None
	best, best_score = None, -1
	for row in frappe.get_cached_doc("Automotive Settings").get("tax_category_defaults") or []:
		if row.party_type != party_type:
			continue
		if row.company_country and row.company_country != company_country:
			continue
		if row.party_country and row.party_country != country:
			continue
		score = (2 if row.party_country else 0) + (1 if row.company_country else 0)
		if score > best_score:
			best, best_score = row.tax_category, score
	return best
