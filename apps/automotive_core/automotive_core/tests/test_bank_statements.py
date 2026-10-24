"""Bank statements from a bank partner become bank transactions and reconcile (ERP-09.2)."""

from __future__ import annotations

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from automotive_core.integration_hub import inbound
from automotive_core.tests.utils import AICHI, make_user

BANK = "Test Statement Bank"
GL = "Test Statement Bank - HAMJ"
ACCOUNT_NO = "TSB-0001-HAMJ"


def statement(message_id: str, entries: list[dict], opening: float = 0, closing: float | None = None,
		account: str = ACCOUNT_NO, currency: str = "JPY") -> bytes:
	closing = opening + sum(e["amount"] for e in entries) if closing is None else closing
	return json.dumps({"message_id": message_id, "message_type": "bank_statement", "data": {
		"statement_id": message_id, "account_number": account, "currency": currency, "from_date": nowdate(),
		"to_date": nowdate(), "opening_balance": opening, "closing_balance": closing,
		"entries": [{"booking_date": nowdate(), **e} for e in entries]}}).encode()


class TestBankStatements(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for bank in (BANK, "Other Test Bank"):
			if not frappe.db.exists("Bank", bank):
				frappe.get_doc({"doctype": "Bank", "bank_name": bank}).insert(ignore_permissions=True)
		if not frappe.db.exists("Account", GL):
			parent = frappe.db.get_value("Account", {"company": AICHI, "account_type": "Bank", "is_group": 1}, "name") \
				or frappe.db.get_value("Account", {"company": AICHI, "account_name": "Bank Accounts"}, "name")
			frappe.get_doc({"doctype": "Account", "account_name": "Test Statement Bank", "company": AICHI,
				"parent_account": parent, "account_type": "Bank", "is_group": 0}).insert(ignore_permissions=True)
		frappe.get_doc({"doctype": "Bank Account", "account_name": "HAMJ Operating", "bank": BANK, "company": AICHI,
			"is_company_account": 1, "account": GL, "bank_account_no": ACCOUNT_NO}).insert(ignore_permissions=True)
		make_user("statements@test-bank.example", [], user_type="Website User")
		frappe.get_doc({"doctype": "Integration Partner", "partner_code": "TEST-BANK", "partner_name": BANK,
			"partner_type": "Bank", "bank": BANK, "adapter": "Local File Drop", "message_format": "JSON",
			"allowed_message_types": "bank_statement", "service_user": "statements@test-bank.example"}).insert(
			ignore_permissions=True)

	def setUp(self):
		frappe.flags.automotive_pending_inbound = []

	def _deposit(self, reference: str, amount: float) -> str:
		income = frappe.db.get_value("Account", {"company": AICHI, "root_type": "Income", "is_group": 0}, "name")
		cost_center = frappe.get_cached_value("Company", AICHI, "cost_center")
		je = frappe.get_doc({"doctype": "Journal Entry", "voucher_type": "Bank Entry", "company": AICHI,
			"posting_date": nowdate(), "cheque_no": reference, "cheque_date": nowdate(), "accounts": [
				{"account": GL, "debit_in_account_currency": amount, "cost_center": cost_center},
				{"account": income, "credit_in_account_currency": amount, "cost_center": cost_center}]})
		je.insert(ignore_permissions=True)
		je.submit()
		return je.name

	def _deliver(self, body: bytes) -> str:
		accepted = inbound.accept("TEST-BANK", body)
		inbound.run_pending_inline()
		return frappe.db.get_value("Integration Inbound Message", accepted["inbound_id"], "status")

	def test_statement_entries_become_transactions_and_match_their_payments(self):
		je = self._deposit("DEP-7781", 120_000)
		entries = [{"entry_id": "TSB-1", "amount": 120_000, "reference": "DEP-7781", "counterparty_name": "Dealer"},
			{"entry_id": "TSB-2", "amount": -550, "description": "ACCOUNT MAINTENANCE FEE"}]
		self.assertEqual(self._deliver(statement("STMT-1", entries, opening=1_000)), "Processed")
		rows = {r.transaction_id: r for r in frappe.get_all("Bank Transaction", filters={"bank_account":
			"HAMJ Operating - " + BANK}, fields=["transaction_id", "status", "deposit", "withdrawal"])}
		self.assertEqual((rows["TSB-1"].status, rows["TSB-1"].deposit), ("Reconciled", 120_000))
		self.assertTrue(frappe.db.get_value("Journal Entry", je, "clearance_date"))
		self.assertEqual((rows["TSB-2"].status, rows["TSB-2"].withdrawal), ("Unreconciled", 550))  # treasury's to resolve
		# a resend under a new message id imports nothing twice
		self.assertEqual(self._deliver(statement("STMT-1-RESEND", entries, opening=1_000)), "Processed")
		self.assertEqual(frappe.db.count("Bank Transaction", {"bank_account": "HAMJ Operating - " + BANK}), 2)
		self.assertTrue(frappe.db.exists("Audit Event", {"event_type": "bank.statement_imported"}))

	def test_inconsistent_or_foreign_statements_are_rejected_whole(self):
		entry = [{"entry_id": "TSB-9", "amount": 5_000, "reference": "X"}]
		self.assertEqual(self._deliver(statement("STMT-BAD-1", entry, opening=0, closing=4_000)), "Rejected")
		self.assertEqual(self._deliver(statement("STMT-BAD-2", entry, account="NOT-OURS")), "Rejected")
		self.assertEqual(self._deliver(statement("STMT-BAD-3", entry, currency="USD")), "Rejected")
		self.assertFalse(frappe.db.exists("Bank Transaction", {"transaction_id": "TSB-9"}))
