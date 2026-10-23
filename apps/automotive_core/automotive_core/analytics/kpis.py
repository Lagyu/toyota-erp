"""Finance KPIs owned by automotive_core: cash collection (DSO) and gross margin."""

from __future__ import annotations

import frappe
from frappe.utils import date_diff, flt

from automotive_core.analytics.kpi import KpiResult, ratio

DEFINITIONS = [
	{
		"kpi_code": "FIN-DSO",
		"kpi_name": "Days Sales Outstanding (cash collection)",
		"domain": "Finance",
		"unit": "Days",
		"direction": "Lower Is Better",
		"target_value": 45,
		"refresh_policy": "Daily",
		"compute_method": "automotive_core.analytics.kpis.days_sales_outstanding",
		"reconciliation_method": "automotive_core.analytics.kpis.reconcile_receivables",
		"owner_role": "Group Controller",
		"formula": "Receivable balance at period end / (credit sales in period / days in period)",
		"source_doctypes": "GL Entry (Receivable accounts), Sales Invoice, Payment Ledger Entry",
		"authorization_filter": "Company User Permission of the caller; company-level KPI (no plant scope).",
		"reconciliation_check": "GL receivable balance equals the Payment Ledger outstanding total at period end (±1 unit).",
	},
	{
		"kpi_code": "FIN-GM",
		"kpi_name": "Gross Margin on Invoiced Sales",
		"domain": "Finance",
		"unit": "Percent",
		"direction": "Higher Is Better",
		"target_value": 18,
		"refresh_policy": "Daily",
		"compute_method": "automotive_core.analytics.kpis.gross_margin",
		"reconciliation_method": "automotive_core.analytics.kpis.reconcile_revenue",
		"owner_role": "Group Controller",
		"formula": "(Income posted by Sales Invoices - Cost of Goods Sold posted in period) / income x 100",
		"source_doctypes": "GL Entry, Account, Sales Invoice",
		"authorization_filter": "Company User Permission of the caller; company-level KPI.",
		"reconciliation_check": "Income in GL from Sales Invoices equals the sum of submitted Sales Invoice base net totals.",
	},
]


def _receivable_balance(company: str, on_date) -> float:
	return flt(
		frappe.db.sql(
			"""select sum(gle.debit - gle.credit) from `tabGL Entry` gle
			join `tabAccount` a on a.name = gle.account
			where gle.company=%s and gle.is_cancelled=0 and gle.posting_date<=%s and a.account_type='Receivable'""",
			(company, on_date),
		)[0][0]
	)


def days_sales_outstanding(company: str, from_date, to_date, plant: str | None = None) -> KpiResult:
	balance = _receivable_balance(company, to_date)
	sales = flt(
		frappe.db.sql(
			"""select sum(base_grand_total) from `tabSales Invoice`
			where company=%s and docstatus=1 and posting_date between %s and %s""",
			(company, from_date, to_date),
		)[0][0]
	)
	days = date_diff(to_date, from_date) + 1
	daily = sales / days if days else 0
	value = round(balance / daily, 2) if daily else None
	return KpiResult(value, balance, daily, {"company": company, "from": str(from_date), "to": str(to_date),
		"receivable": balance, "credit_sales": sales})


def reconcile_receivables(company: str, from_date, to_date, plant, result: KpiResult) -> tuple[bool, str]:
	ple = flt(
		frappe.db.sql(
			"""select sum(amount) from `tabPayment Ledger Entry`
			where company=%s and account_type='Receivable' and delinked=0 and posting_date<=%s""",
			(company, to_date),
		)[0][0]
	)
	gl = flt(result.numerator)
	ok = abs(gl - ple) <= 1
	return ok, f"GL receivable {gl:.2f} vs payment ledger {ple:.2f}"


def _income_from_sales_invoices(company: str, from_date, to_date) -> float:
	return flt(
		frappe.db.sql(
			"""select sum(gle.credit - gle.debit) from `tabGL Entry` gle
			join `tabAccount` a on a.name = gle.account
			where gle.company=%s and gle.is_cancelled=0 and gle.posting_date between %s and %s
			and a.root_type='Income' and gle.voucher_type='Sales Invoice'""",
			(company, from_date, to_date),
		)[0][0]
	)


def gross_margin(company: str, from_date, to_date, plant: str | None = None) -> KpiResult:
	income = _income_from_sales_invoices(company, from_date, to_date)
	cogs = flt(
		frappe.db.sql(
			"""select sum(gle.debit - gle.credit) from `tabGL Entry` gle
			join `tabAccount` a on a.name = gle.account
			where gle.company=%s and gle.is_cancelled=0 and gle.posting_date between %s and %s
			and a.account_type='Cost of Goods Sold'""",
			(company, from_date, to_date),
		)[0][0]
	)
	return KpiResult(ratio(income - cogs, income), income - cogs, income,
		{"company": company, "from": str(from_date), "to": str(to_date), "income": income, "cogs": cogs})


def reconcile_revenue(company: str, from_date, to_date, plant, result: KpiResult) -> tuple[bool, str]:
	invoices = flt(
		frappe.db.sql(
			"""select sum(base_net_total) from `tabSales Invoice`
			where company=%s and docstatus=1 and posting_date between %s and %s""",
			(company, from_date, to_date),
		)[0][0]
	)
	income = flt(result.denominator)
	count = frappe.db.count("Sales Invoice", {"company": company, "docstatus": 1,
		"posting_date": ("between", (from_date, to_date))})
	ok = abs(income - invoices) <= max(1, count)
	return ok, f"GL income {income:.2f} vs invoice net totals {invoices:.2f} ({count} invoices)"
