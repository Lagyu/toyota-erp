"""Finance flows through ERPNext's own documents: the settlement bank's daily statements
and their reconciliation, month-end foreign-currency revaluation and the fiscal year-end
stock closing and period closing voucher."""

from __future__ import annotations

import calendar
import datetime as dt
import json

import frappe
from frappe.utils import flt

from automotive_seed.generator.catalog import AICHI, KYUSHU, NA
from automotive_seed.generator.flows.common import COMPANY_ABBR, company_bank_account

FISCAL_YEAR_ENDS = {(3, 31)}
BANK = "Hinode Settlement Bank"
BANK_PARTNER = "BANK-HSB"
MONTHLY_FEE = {AICHI: 2_200, KYUSHU: 2_200, NA: 35}  # account maintenance, in the account currency
PLANT_OF = {AICHI: "AICHI-VA", KYUSHU: "KYUSHU-PT", NA: "NA-PDC"}


class Finance:
	def __init__(self, sim):
		self.sim = sim
		self.ctx = sim.ctx
		self.s = sim.state["sim"]

	def daily(self, day: dt.date) -> None:
		last = calendar.monthrange(day.year, day.month)[1]
		self.bank_statements(day, month_end=day.day == last)
		if day.day == last:
			self.revalue(day)
		if (day.month, day.day) in FISCAL_YEAR_ENDS:
			self.close_year(day)

	# -----------------------------------------------------------------------------------
	# bank statements
	# -----------------------------------------------------------------------------------
	def _bank_partner(self) -> str:
		"""The settlement bank as an integration partner (created on first use)."""
		if not frappe.db.exists("Bank", BANK):
			frappe.get_doc({"doctype": "Bank", "bank_name": BANK}).insert(ignore_permissions=True)
		if not frappe.db.exists("Integration Partner", BANK_PARTNER):
			user = "statements@settlement-bank.example"
			if not frappe.db.exists("User", user):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": user,
						"first_name": "Statements",
						"last_name": BANK,
						"user_type": "Website User",
						"send_welcome_email": 0,
					}
				).insert(ignore_permissions=True)
			frappe.get_doc(
				{
					"doctype": "Integration Partner",
					"partner_code": BANK_PARTNER,
					"partner_name": BANK,
					"partner_type": "Bank",
					"bank": BANK,
					"adapter": "HTTPS Webhook",
					"message_format": "JSON",
					"allowed_message_types": "bank_statement",
					"service_user": user,
				}
			).insert(ignore_permissions=True)
		return BANK_PARTNER

	def _company_account(self, company: str) -> tuple[str, str, str]:
		"""(Bank Account, its number, GL account) of the company's settlement account."""
		gl = company_bank_account(company)
		abbr = COMPANY_ABBR[company]
		number = f"HSB-{abbr}-0001"
		name = frappe.db.get_value(
			"Bank Account", {"bank_account_no": number, "is_company_account": 1}, "name"
		)
		if not name:
			name = (
				frappe.get_doc(
					{
						"doctype": "Bank Account",
						"account_name": f"{abbr} Settlement",
						"bank": BANK,
						"company": company,
						"is_company_account": 1,
						"account": gl,
						"bank_account_no": number,
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
		return name, number, gl

	def bank_statements(self, day: dt.date, month_end: bool = False) -> None:
		"""The bank's statement of each company account for the day, delivered through the
		integration hub, which imports it and matches its entries against our payment entries.
		Month-end account fees have no payment of ours to match: AP books each as a bank entry
		from the statement line (the Bank Reconciliation Tool's own path)."""
		from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
			create_bank_entry_and_reconcile,
		)

		from automotive_core.integration_hub import inbound

		ctx = self.ctx
		partner = self._bank_partner()
		balances = self.s.setdefault("bank_balance", {})
		for company in (AICHI, KYUSHU, NA):
			bank_account, number, gl = self._company_account(company)
			abbr = COMPANY_ABBR[company]
			entries = []
			for pe in frappe.db.sql(
				"""select name, paid_from, paid_to, paid_amount, received_amount, reference_no,
					party_name from `tabPayment Entry` where docstatus = 1 and company = %s and posting_date = %s
					and (paid_from = %s or paid_to = %s) order by name""",
				(company, day, gl, gl),
				as_dict=True,
			):
				amount = flt(pe.received_amount) if pe.paid_to == gl else -flt(pe.paid_amount)
				entries.append(
					{
						"entry_id": f"HSB-{abbr}-{pe.name}",
						"booking_date": str(day),
						"amount": amount,
						"reference": pe.reference_no,
						"counterparty_name": pe.party_name,
					}
				)
			fee_id = f"HSB-{abbr}-FEE-{day:%Y%m}"
			if month_end:
				entries.append(
					{
						"entry_id": fee_id,
						"booking_date": str(day),
						"amount": -MONTHLY_FEE[company],
						"description": "ACCOUNT MAINTENANCE FEE",
					}
				)
			if not entries:
				continue
			if company not in balances:  # the bank's balance before its first statement is our ledger's
				balances[company] = flt(
					frappe.db.sql(
						"""select sum(debit_in_account_currency)
					- sum(credit_in_account_currency) from `tabGL Entry` where account = %s and is_cancelled = 0
					and posting_date < %s""",
						(gl, day),
					)[0][0]
				)
			opening = balances[company]
			closing = round(opening + sum(e["amount"] for e in entries), 2)
			body = json.dumps(
				{
					"message_id": f"STMT-{abbr}-{day.isoformat()}",
					"message_type": "bank_statement",
					"data": {
						"statement_id": f"{number}/{day.isoformat()}",
						"account_number": number,
						"currency": frappe.get_cached_value("Account", gl, "account_currency"),
						"from_date": str(day),
						"to_date": str(day),
						"opening_balance": opening,
						"closing_balance": closing,
						"entries": entries,
					},
				}
			)
			with ctx.step(23, 30):
				inbound.accept(partner, body, source="HTTPS")
				inbound.run_pending_inline()
			ctx.count("Bank Transaction", len(entries))
			balances[company] = closing
			fee = month_end and frappe.db.get_value(
				"Bank Transaction",
				{
					"bank_account": bank_account,
					"transaction_id": fee_id,
					"status": "Unreconciled",
					"docstatus": 1,
				},
				"name",
			)
			if fee:
				with ctx.step(23, 45), ctx.as_user(self.sim.person("AP Clerk")):
					create_bank_entry_and_reconcile(
						fee,
						day,
						day,
						fee_id,
						[
							{"account": gl, "bank_account": bank_account, "credit": MONTHLY_FEE[company]},
							{"account": f"Bank Charges - {abbr}", "debit": MONTHLY_FEE[company]},
						],
						user_remark="Account maintenance fee (bank statement)",
					)
				ctx.count("Journal Entry")

	def revalue(self, day: dt.date) -> None:
		"""Exchange Rate Revaluation of foreign-currency balances (e.g. Aichi's USD receivable from NA)."""
		ctx = self.ctx
		for company in (AICHI, KYUSHU):
			with ctx.step(22, 0):
				with ctx.as_user(self.sim.person("Group Controller")):
					err = frappe.new_doc("Exchange Rate Revaluation")
					err.company = company
					err.posting_date = day
					err.rounding_loss_allowance = 0.05
					try:
						err.fetch_and_calculate_accounts_data()
					except frappe.ValidationError:
						continue  # no foreign-currency balances to revalue
					if not err.accounts:
						continue
					err.insert()
					err.submit()
					result = err.make_jv_entries() or {}
					for key in ("revaluation_jv", "zero_balance_jv"):
						if result.get(key):
							je = frappe.get_doc("Journal Entry", result[key])
							if je.docstatus == 0:
								je.submit()
				ctx.count("Exchange Rate Revaluation")

	def close_stock(self, company: str, day: dt.date) -> None:
		"""ERPNext closes the stock balances before the books: a Stock Closing Entry of the whole
		company up to the year end, by its plant's director, once the day's queued reposts are
		processed (its balance job, queued at submit, runs at once like every job of a run)."""
		from automotive_seed.runner import _repost_pending

		if frappe.db.exists("Stock Closing Entry", {"company": company, "to_date": day, "docstatus": 1}):
			return
		_repost_pending()
		with self.ctx.as_user(self.sim.person("Plant Director", plant=PLANT_OF[company])):
			entry = frappe.get_doc({"doctype": "Stock Closing Entry", "company": company, "to_date": day})
			entry.insert()
			entry.submit()
		status = frappe.db.get_value("Stock Closing Entry", entry.name, "status")
		if status != "Completed":
			raise RuntimeError(f"Stock Closing Entry {entry.name} of {company} ended {status}")
		self.ctx.count("Stock Closing Entry")

	def close_year(self, day: dt.date) -> None:
		ctx = self.ctx
		for company in (AICHI, KYUSHU, NA):
			fy = frappe.db.get_value("Fiscal Year", {"year_end_date": day}, "name")
			if not fy or frappe.db.exists(
				"Period Closing Voucher", {"company": company, "fiscal_year": fy, "docstatus": 1}
			):
				continue
			closing = frappe.db.get_value(
				"Account",
				{
					"company": company,
					"root_type": "Liability",
					"is_group": 0,
					"account_name": ("like", "%Retained%"),
				},
				"name",
				order_by="name asc",
			) or frappe.db.get_value(
				"Account",
				{"company": company, "root_type": "Equity", "is_group": 0},
				"name",
				order_by="name asc",
			)
			with ctx.step(23, 0):
				self.close_stock(company, day)
			with ctx.step(23, 30):
				with ctx.as_user(self.sim.person("Group Controller")):
					pcv = frappe.get_doc(
						{
							"doctype": "Period Closing Voucher",
							"company": company,
							"fiscal_year": fy,
							"transaction_date": day,
							"period_start_date": frappe.db.get_value("Fiscal Year", fy, "year_start_date"),
							"period_end_date": day,
							"closing_account_head": closing,
							"remarks": f"Year-end close {fy}",
						}
					)
					pcv.insert()
					pcv.submit()
			ctx.count("Period Closing Voucher")
