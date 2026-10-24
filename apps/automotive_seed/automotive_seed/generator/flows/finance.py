"""Finance flows: month-end foreign-currency revaluation and the fiscal year-end
period closing voucher, through ERPNext's own documents."""

from __future__ import annotations

import calendar
import datetime as dt

import frappe

from automotive_seed.generator.catalog import AICHI, KYUSHU, NA

FISCAL_YEAR_ENDS = {(3, 31)}


class Finance:
	def __init__(self, sim):
		self.sim = sim
		self.ctx = sim.ctx
		self.s = sim.state["sim"]

	def daily(self, day: dt.date) -> None:
		last = calendar.monthrange(day.year, day.month)[1]
		if day.day == last:
			self.revalue(day)
		if (day.month, day.day) in FISCAL_YEAR_ENDS:
			self.close_year(day)

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

	def close_year(self, day: dt.date) -> None:
		ctx = self.ctx
		for company in (AICHI, KYUSHU, NA):
			fy = frappe.db.get_value("Fiscal Year", {"year_end_date": day}, "name")
			if not fy or frappe.db.exists("Period Closing Voucher", {"company": company, "fiscal_year": fy, "docstatus": 1}):
				continue
			closing = frappe.db.get_value("Account", {"company": company, "root_type": "Liability", "is_group": 0,
				"account_name": ("like", "%Retained%")}, "name") or frappe.db.get_value("Account", {"company": company,
				"root_type": "Equity", "is_group": 0}, "name")
			with ctx.step(23, 30):
				with ctx.as_user(self.sim.person("Group Controller")):
					pcv = frappe.get_doc({"doctype": "Period Closing Voucher", "company": company, "fiscal_year": fy,
						"transaction_date": day, "period_start_date": frappe.db.get_value("Fiscal Year", fy, "year_start_date"),
						"period_end_date": day, "closing_account_head": closing,
						"remarks": f"Year-end close {fy}"})
					pcv.insert()
					pcv.submit()
			ctx.count("Period Closing Voucher")
