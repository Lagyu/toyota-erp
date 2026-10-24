"""Bank statements from a bank partner (ERP-09.2 bank reconciliation, ERP-11.2 inbound).

A statement for one of our accounts at the partner's bank (its balances and entries)
becomes submitted Bank Transactions. ERPNext's automatic reconciliation then matches each
against the payment or journal entry with the same reference and amount. Entries without a
match stay Unreconciled for treasury to resolve in the Bank Reconciliation Tool.

The whole statement is rejected if:
- its balances do not add up;
- its dates do not cover its entries;
- the account is held at another bank;
- the currency differs from the account's.

Entry ids make resends idempotent: an entry already imported for the account is skipped.
"""

from __future__ import annotations

from datetime import date

import frappe
from frappe import _
from frappe.utils import add_days, flt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from automotive_core.governance import audit
from automotive_core.integration_hub.inbound import InboundRejected


class Strict(BaseModel):
	model_config = ConfigDict(extra="forbid")


class StatementEntry(Strict):
	entry_id: str = Field(min_length=1, max_length=140)
	booking_date: date
	amount: float  # signed: + credited to our account, - debited
	reference: str | None = None
	counterparty_name: str | None = None
	counterparty_account: str | None = None
	description: str | None = None


class BankStatementMessage(Strict):
	statement_id: str = Field(min_length=1, max_length=140)
	account_number: str
	currency: str = Field(min_length=3, max_length=3)
	from_date: date
	to_date: date
	opening_balance: float
	closing_balance: float
	entries: list[StatementEntry] = []


# payments are often booked a few days before the bank executes them
MATCH_LOOKBACK_DAYS = 14


def _parse(data) -> BankStatementMessage:
	try:
		return BankStatementMessage.model_validate(data)
	except ValidationError as exc:
		raise InboundRejected(f"invalid BankStatementMessage: {exc}") from exc


def bank_statement(message, partner, data):
	from automotive_core.governance.elevation import on_behalf_of

	msg = _parse(data)
	if not partner.get("bank"):
		raise InboundRejected(f"partner {partner.name} is not linked to a bank")
	account = frappe.db.get_value(
		"Bank Account",
		{"bank_account_no": msg.account_number, "is_company_account": 1, "bank": partner.bank, "disabled": 0},
		["name", "company", "account"],
		as_dict=True,
	)
	if not account:
		raise InboundRejected(f"no company account {msg.account_number} at {partner.bank}")
	currency = frappe.get_cached_value("Account", account.account, "account_currency")
	if msg.currency != currency:
		raise InboundRejected(f"statement currency {msg.currency} differs from the account's {currency}")
	if msg.from_date > msg.to_date or any(
		not msg.from_date <= e.booking_date <= msg.to_date for e in msg.entries
	):
		raise InboundRejected("entries fall outside the statement period")
	movement = sum(e.amount for e in msg.entries)
	if abs(flt(msg.opening_balance + movement - msg.closing_balance, 2)) >= 0.01:
		raise InboundRejected(
			f"balances do not add up: {msg.opening_balance} + {movement} != {msg.closing_balance}"
		)
	with on_behalf_of(
		_("Bank statement {0} from {1}").format(msg.statement_id, partner.partner_name),
		"Integration Inbound Message",
		message.name,
	):
		created = []
		for entry in msg.entries:
			if not flt(entry.amount) or frappe.db.exists(
				"Bank Transaction",
				{"bank_account": account.name, "transaction_id": entry.entry_id, "docstatus": 1},
			):
				continue
			transaction = frappe.get_doc(
				{
					"doctype": "Bank Transaction",
					"date": entry.booking_date,
					"bank_account": account.name,
					"company": account.company,
					"currency": currency,
					"deposit": max(flt(entry.amount), 0),
					"withdrawal": max(-flt(entry.amount), 0),
					"description": entry.description,
					"reference_number": entry.reference,
					"transaction_id": entry.entry_id,
					"bank_party_name": entry.counterparty_name,
					"bank_party_account_number": entry.counterparty_account,
				}
			)
			transaction.insert()
			transaction.submit()
			created.append(transaction)
		reconciled = reconcile(created, add_days(msg.from_date, -MATCH_LOOKBACK_DAYS), msg.to_date)
	audit.record(
		"bank.statement_imported",
		"Bank Account",
		account.name,
		{
			"statement": msg.statement_id,
			"entries": len(msg.entries),
			"imported": len(created),
			"reconciled": reconciled,
			"closing_balance": msg.closing_balance,
		},
		company=account.company,
	)
	return "Bank Account", account.name


def reconcile(transactions, from_date, to_date) -> int:
	"""ERPNext's automatic matching (reference and amount) for these transactions; returns how
	many are fully reconciled."""
	from erpnext.accounts.doctype.bank_reconciliation_tool.bank_reconciliation_tool import (
		start_auto_reconcile,
	)

	if not transactions:
		return 0
	start_auto_reconcile(transactions, from_date, to_date, None, None, None)
	frappe.clear_messages()  # its summary is meant for a desk user
	names = [t.name for t in transactions]
	return frappe.db.count("Bank Transaction", {"name": ("in", names), "status": "Reconciled"})
