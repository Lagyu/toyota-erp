"""ERP-09 period close: checks, waivers, maker/checker approval and the period lock."""

from __future__ import annotations

import frappe
from erpnext.accounts.doctype.accounting_period.accounting_period import ClosedAccountingPeriod
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_months, get_first_day, get_last_day, nowdate

from automotive_core.finance_controls import period_close
from automotive_core.governance.cancellation import cancel_with_reason
from automotive_core.tests.utils import AICHI, approve, as_user, make_user

CONTROLLER = "controller1.close@hinode.example"
CONTROLLER2 = "controller2.close@hinode.example"


class TestPeriodClose(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(CONTROLLER, ["Group Controller", "Accounts Manager"])
		make_user(CONTROLLER2, ["Group Controller", "Accounts Manager"])
		month = add_months(get_first_day(nowdate()), -3)
		cls.start, cls.end = month, get_last_day(month)
		cls.fiscal_year = frappe.db.get_value(
			"Fiscal Year", {"year_start_date": ("<=", cls.start), "year_end_date": (">=", cls.end)}, "name"
		)

	def _journal(self, day, submit=True):
		accounts = frappe.get_all(
			"Account",
			filters={"company": AICHI, "is_group": 0, "account_type": "Cash"},
			pluck="name",
			limit=1,
		) or frappe.get_all(
			"Account",
			filters={"company": AICHI, "is_group": 0, "root_type": "Asset", "account_type": ""},
			pluck="name",
			limit=1,
		)
		equity = frappe.db.get_value("Account", {"company": AICHI, "account_name": "Capital Stock"}, "name")
		je = frappe.get_doc(
			{
				"doctype": "Journal Entry",
				"company": AICHI,
				"posting_date": day,
				"voucher_type": "Opening Entry",
				"is_opening": "Yes",
				"accounts": [
					{"account": accounts[0], "debit_in_account_currency": 100},
					{"account": equity, "credit_in_account_currency": 100},
				],
			}
		)
		je.insert(ignore_permissions=True)
		if submit:
			je.submit()
		return je

	def test_close_locks_the_period_and_reopen_unlocks_it(self):
		with as_user(CONTROLLER):
			doc = frappe.get_doc(
				{
					"doctype": "Period Close Checklist",
					"company": AICHI,
					"fiscal_year": self.fiscal_year,
					"period_start": self.start,
					"period_end": self.end,
				}
			).insert()
			self.assertEqual(
				{t.check_code for t in doc.tasks}
				>= {"TB-BALANCED", "AUDIT-CHAIN", "STOCK-GL", "MATCH-EXCEPTIONS"},
				True,
			)
			status = period_close.run_checks(doc.name)
			doc.reload()
			for task in doc.tasks:
				if task.status == "Failed":
					waivable = {c: w for c, _d, w, _p in period_close.registered_checks()}[task.check_code]
					self.assertTrue(waivable, f"{task.check_code} failed on the test site: {task.result}")
					period_close.waive(
						doc.name, task.check_code, "Test site: reviewed and accepted by controller"
					)
			doc.reload()
			self.assertEqual(doc.status, "Ready For Approval", status)
			with self.assertRaises(frappe.ValidationError):  # needs the second controller's approval
				doc.submit()
		approve(doc, CONTROLLER, [CONTROLLER2])
		with as_user(CONTROLLER):
			doc.reload()
			doc.submit()
		doc.reload()
		self.assertEqual(doc.status, "Closed")
		self.assertTrue(doc.accounting_period)
		with self.assertRaises(ClosedAccountingPeriod):
			self._journal(add_days(self.start, 3))
		with as_user(CONTROLLER):
			cancel_with_reason("Period Close Checklist", doc.name, "Late supplier credit note must be booked")
		self.assertEqual(frappe.db.get_value("Accounting Period", doc.accounting_period, "disabled"), 1)
		self.assertEqual(self._journal(add_days(self.start, 3)).docstatus, 1)

	def test_hard_checks_cannot_be_waived(self):
		start = add_months(self.start, -1)
		fiscal_year = frappe.db.get_value(
			"Fiscal Year",
			{"year_start_date": ("<=", start), "year_end_date": (">=", get_last_day(start))},
			"name",
		)
		with as_user(CONTROLLER):
			doc = frappe.get_doc(
				{
					"doctype": "Period Close Checklist",
					"company": AICHI,
					"fiscal_year": fiscal_year,
					"period_start": start,
					"period_end": get_last_day(start),
				}
			).insert()
			with self.assertRaises(frappe.ValidationError):
				period_close.waive(doc.name, "TB-BALANCED", "This must never be possible")
