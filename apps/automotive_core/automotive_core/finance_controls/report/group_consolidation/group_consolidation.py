# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Group consolidation view (ERP-09): each company's general ledger by root type,
translated into the presentation currency — balance sheet at the closing rate, profit
and loss at the period's average rate — with the intercompany portion identified.

Every figure is a GL Entry sum; nothing is entered by hand. The translation difference
line is what translating at two rates leaves over (it is zero in company currency).
This is a management view: unrealised profit in intercompany inventory is not
eliminated (see DECISIONS.md, D-021).
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate, nowdate

from automotive_core.analytics import scope

BALANCE_SHEET = ("Asset", "Liability", "Equity")
PROFIT_AND_LOSS = ("Income", "Expense")
CREDIT_NATURE = ("Liability", "Equity", "Income")

INTERNAL = """(
	(gle.party_type = 'Customer' and gle.party in (select name from `tabCustomer` where is_internal_customer = 1))
	or (gle.party_type = 'Supplier' and gle.party in (select name from `tabSupplier` where is_internal_supplier = 1))
	or (gle.voucher_type in ('Sales Invoice', 'Delivery Note') and gle.voucher_no in (
		select x.name from `tabSales Invoice` x join `tabCustomer` c on c.name = x.customer
		where c.is_internal_customer = 1
		union select y.name from `tabDelivery Note` y join `tabCustomer` c2 on c2.name = y.customer
		where c2.is_internal_customer = 1))
	or (gle.voucher_type in ('Purchase Invoice', 'Purchase Receipt') and gle.voucher_no in (
		select p.name from `tabPurchase Invoice` p join `tabSupplier` s on s.name = p.supplier
		where s.is_internal_supplier = 1
		union select q.name from `tabPurchase Receipt` q join `tabSupplier` s2 on s2.name = q.supplier
		where s2.is_internal_supplier = 1)))"""


def rate(from_currency: str, to_currency: str, on_date, average_from=None) -> float:
	if from_currency == to_currency:
		return 1.0
	for a, b, invert in ((from_currency, to_currency, False), (to_currency, from_currency, True)):
		if average_from:
			value = frappe.db.sql("""select avg(exchange_rate) from `tabCurrency Exchange` where from_currency=%s
				and to_currency=%s and date between %s and %s""", (a, b, average_from, on_date))[0][0]
		else:
			value = frappe.db.get_value("Currency Exchange", {"from_currency": a, "to_currency": b,
				"date": ("<=", on_date)}, "exchange_rate", order_by="date desc")
		if value:
			return 1 / flt(value) if invert else flt(value)
	if average_from:
		return rate(from_currency, to_currency, on_date)
	frappe.throw(_("No exchange rate {0} → {1} on or before {2}.").format(from_currency, to_currency, on_date))


def _sums(company: str, from_date, to_date, internal: bool) -> dict[str, float]:
	"""Debit-minus-credit by root type: balance sheet accounts cumulatively to ``to_date``,
	profit and loss for ``from_date``..``to_date``; ``internal`` keeps only intercompany rows."""
	clause = f" and {INTERNAL}" if internal else ""
	rows = frappe.db.sql(f"""select a.root_type, sum(gle.debit - gle.credit)
		from `tabGL Entry` gle join `tabAccount` a on a.name = gle.account
		where gle.company = %(company)s and gle.is_cancelled = 0 and gle.posting_date <= %(to)s
		and (a.root_type in ('Asset', 'Liability', 'Equity') or gle.posting_date >= %(from)s)
		{clause}
		group by a.root_type""", {"company": company, "from": from_date, "to": to_date})  # noqa: S608 (constant SQL fragments; values are bound)
	return {root: flt(value) for root, value in rows}


def _period_start(filters, to_date, companies: list[str]):
	if filters.from_date:
		return getdate(filters.from_date)
	from erpnext.accounts.utils import get_fiscal_year

	return getdate(get_fiscal_year(to_date, company=companies[0] if companies else None)[1])


def execute(filters=None):
	filters = frappe._dict(filters or {})
	to_date = getdate(filters.to_date or nowdate())
	currency = filters.presentation_currency or "JPY"
	companies = scope.companies(filters)
	from_date = _period_start(filters, to_date, companies)
	columns = [{"fieldname": "line", "label": _("Statement Line"), "fieldtype": "Data", "width": 260}]
	for company in companies:
		columns.append({"fieldname": scrub(company), "label": frappe.get_cached_value("Company", company, "abbr"),
			"fieldtype": "Currency", "options": "currency", "width": 160})
	columns += [
		{"fieldname": "group_total", "label": _("Group Total"), "fieldtype": "Currency", "options": "currency",
			"width": 170},
		{"fieldname": "intercompany", "label": _("of which Intercompany"), "fieldtype": "Currency",
			"options": "currency", "width": 170},
		{"fieldname": "external", "label": _("Group excl. Intercompany"), "fieldtype": "Currency",
			"options": "currency", "width": 190},
	]
	lines = {root: {"line": _(root), "currency": currency} for root in (*BALANCE_SHEET, *PROFIT_AND_LOSS)}
	net = {"line": _("Net Profit (Income - Expense)"), "currency": currency, "bold": 1}
	translation = {"line": _("Currency Translation Difference"), "currency": currency}
	for company in companies:
		key = scrub(company)
		company_currency = frappe.get_cached_value("Company", company, "default_currency")
		closing = rate(company_currency, currency, to_date)
		average = rate(company_currency, currency, to_date, average_from=from_date)
		totals, internal = _sums(company, from_date, to_date, False), _sums(company, from_date, to_date, True)
		translated_total = 0.0
		for root, row in lines.items():
			sign = -1 if root in CREDIT_NATURE else 1
			factor = closing if root in BALANCE_SHEET else average
			value = sign * totals.get(root, 0.0) * factor
			row[key] = value
			row["group_total"] = row.get("group_total", 0.0) + value
			row["intercompany"] = row.get("intercompany", 0.0) + sign * internal.get(root, 0.0) * factor
			translated_total += (1 if root in ("Asset", "Expense") else -1) * value
		net[key] = lines["Income"][key] - lines["Expense"][key]
		# debits = credits in company currency; what remains after translation is the CTA
		translation[key] = -translated_total
	for row in (*lines.values(), net, translation):
		if row is net:
			row["group_total"] = sum(net[scrub(c)] for c in companies)
			row["intercompany"] = lines["Income"]["intercompany"] - lines["Expense"]["intercompany"]
		elif row is translation:
			row["group_total"] = sum(translation[scrub(c)] for c in companies)
			row["intercompany"] = 0.0
		row["external"] = row.get("group_total", 0.0) - row.get("intercompany", 0.0)
	return columns, [*lines.values(), net, translation]


def scrub(text: str) -> str:
	return frappe.scrub(text)
