"""Companies set up before the tenant created their unrealized exchange gain/loss account get
it: without one, the month-end Exchange Rate Revaluation cannot post its journal entry."""

import frappe

from automotive_core.setup.tenant import TenantLog, revaluation_account


def execute():
	log = TenantLog(dry_run=False)
	for company in frappe.get_all("Company", pluck="name"):
		revaluation_account(company, log)
