"""Period close (ERP-09): automated ledger checks, controller waivers with reasons,
maker/checker approval, and a closed ERPNext Accounting Period on submit.

Checks are plain functions ``check(company, period_start, period_end) -> (ok, result)``
registered as ``(code, description, waivable, path)`` through the
``automotive_period_close_checks`` hook, so each app contributes the checks for its own
ledgers. A failed check blocks submission unless it is waivable and a Group Controller
waived it with a reason (audited). Submitting creates (or re-enables) an ERPNext
Accounting Period that closes every period-closing document type; cancelling the
checklist (with a reason) re-opens it by disabling that period — the record stays.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, now_datetime

from automotive_core.governance import audit

CORE_CHECKS = [
	("TB-BALANCED", "Trial balance: debits equal credits up to period end", False,
		"automotive_core.finance_controls.period_close.check_trial_balance"),
	("NO-DRAFTS", "No draft invoices, payments or journals dated in the period", True,
		"automotive_core.finance_controls.period_close.check_drafts"),
	("AR-AP-LEDGER", "Receivables and payables in the GL equal the payment ledger", False,
		"automotive_core.finance_controls.period_close.check_party_ledgers"),
	("STOCK-GL", "Stock ledger value equals the stock accounts in the GL", False,
		"automotive_core.finance_controls.period_close.check_stock_vs_gl"),
	("FX-REVALUED", "Foreign-currency balances revalued at period end", True,
		"automotive_core.finance_controls.period_close.check_fx_revaluation"),
	("AUDIT-CHAIN", "Audit events sealed and the hash chain verifies", False,
		"automotive_core.finance_controls.period_close.check_audit_chain"),
]


def registered_checks() -> list[tuple[str, str, bool, str]]:
	checks = list(CORE_CHECKS)
	for item in frappe.get_hooks("automotive_period_close_checks") or []:
		checks.append(tuple(item) if not isinstance(item, str) else tuple(frappe.get_attr(item)))
	return checks


# ---------------------------------------------------------------------------------------
# Core checks


def check_trial_balance(company, period_start, period_end):
	debit, credit = frappe.db.sql("""select ifnull(sum(debit), 0), ifnull(sum(credit), 0) from `tabGL Entry`
		where company = %s and is_cancelled = 0 and posting_date <= %s""", (company, period_end))[0]
	return abs(flt(debit) - flt(credit)) < 0.01, _("debit {0}, credit {1}").format(flt(debit), flt(credit))


def check_drafts(company, period_start, period_end):
	drafts = []
	for doctype in ("Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry"):
		names = frappe.get_all(doctype, filters={"company": company, "docstatus": 0,
			"posting_date": ("between", (period_start, period_end))}, pluck="name", limit=5)
		drafts += [f"{doctype} {n}" for n in names]
	return not drafts, ", ".join(drafts) or _("none")


def check_party_ledgers(company, period_start, period_end):
	problems = []
	for account_type in ("Receivable", "Payable"):
		gl = flt(frappe.db.sql("""select sum(gle.debit - gle.credit) from `tabGL Entry` gle join `tabAccount` a
			on a.name = gle.account where gle.company = %s and gle.is_cancelled = 0 and a.account_type = %s
			and gle.posting_date <= %s""", (company, account_type, period_end))[0][0])
		ple = flt(frappe.db.sql("""select sum(amount) from `tabPayment Ledger Entry` where company = %s
			and account_type = %s and delinked = 0 and posting_date <= %s""",
			(company, account_type, period_end))[0][0])
		if abs(gl - ple) > 1:
			problems.append(_("{0}: GL {1} vs payment ledger {2}").format(account_type, gl, ple))
	return not problems, "; ".join(problems) or _("reconciled")


def check_stock_vs_gl(company, period_start, period_end):
	sle = flt(frappe.db.sql("""select sum(stock_value_difference) from `tabStock Ledger Entry` where company = %s
		and is_cancelled = 0 and posting_date <= %s""", (company, period_end))[0][0])
	accounts = frappe.db.sql_list("""select distinct ifnull(w.account, c.default_inventory_account) from `tabWarehouse` w
		join `tabCompany` c on c.name = w.company where w.company = %s and w.is_group = 0""", company)
	accounts = [a for a in accounts if a]
	gl = flt(frappe.db.sql("""select sum(debit - credit) from `tabGL Entry` where company = %s and is_cancelled = 0
		and posting_date <= %s and account in %s""", (company, period_end, tuple(accounts) or ("",)))[0][0]) \
		if accounts else 0.0
	return abs(sle - gl) <= max(1.0, abs(sle) * 1e-7), _("stock ledger {0} vs GL {1}").format(round(sle, 2), round(gl, 2))


def check_fx_revaluation(company, period_start, period_end):
	currency = frappe.get_cached_value("Company", company, "default_currency")
	foreign = frappe.db.sql("""select count(*) from `tabGL Entry` gle join `tabAccount` a on a.name = gle.account
		where gle.company = %s and gle.is_cancelled = 0 and gle.posting_date <= %s
		and a.account_currency != %s and a.account_type in ('Receivable', 'Payable', 'Bank', 'Cash')""",
		(company, period_end, currency))[0][0]
	if not foreign:
		return True, _("no foreign-currency monetary balances")
	revaluation = frappe.db.get_value("Exchange Rate Revaluation", {"company": company, "docstatus": 1,
		"posting_date": ("between", (period_start, add_days(period_end, 1)))}, "name")
	return bool(revaluation), revaluation or _("no submitted Exchange Rate Revaluation in the period")


def check_audit_chain(company, period_start, period_end):
	from automotive_core.governance.audit import seal_pending, verify_chain

	while seal_pending():
		pass
	result = verify_chain()
	return bool(result["ok"]), _("{0} events checked").format(result.get("checked"))


# ---------------------------------------------------------------------------------------
# Workflow


def validate(doc) -> None:
	if getdate(doc.period_start) > getdate(doc.period_end):
		frappe.throw(_("Period start must not be after period end."))
	start, end = frappe.db.get_value("Fiscal Year", doc.fiscal_year, ["year_start_date", "year_end_date"])
	if not (getdate(start) <= getdate(doc.period_start) and getdate(doc.period_end) <= getdate(end)):
		frappe.throw(_("The period must lie within fiscal year {0}.").format(doc.fiscal_year))
	overlap = frappe.db.get_value("Period Close Checklist", {"company": doc.company, "docstatus": 1,
		"name": ("!=", doc.name), "period_start": ("<=", doc.period_end), "period_end": (">=", doc.period_start)},
		"name")
	if overlap:
		frappe.throw(_("{0} already closes an overlapping period for {1}.").format(overlap, doc.company))
	if not doc.tasks:
		for code, description, _waivable, _path in registered_checks():
			doc.append("tasks", {"check_code": code, "description": description, "status": "Pending"})


@frappe.whitelist(methods=["POST"])
def run_checks(name: str) -> str:
	frappe.only_for(("Group Controller", "System Manager"))
	doc = frappe.get_doc("Period Close Checklist", name)
	if doc.docstatus != 0:
		frappe.throw(_("{0} is already submitted.").format(name))
	by_code = {code: (description, waivable, path) for code, description, waivable, path in registered_checks()}
	existing = {t.check_code: t for t in doc.tasks}
	for code, (description, _waivable, path) in by_code.items():
		task = existing.get(code) or doc.append("tasks", {"check_code": code, "description": description})
		ok, result = frappe.get_attr(path)(doc.company, getdate(doc.period_start), getdate(doc.period_end))
		if task.status == "Waived" and not ok:
			task.result = str(result)[:1000]
			continue
		task.status = "Passed" if ok else "Failed"
		task.result = str(result)[:1000]
		task.waiver_reason, task.waived_by = None, None
	doc.status = "Ready For Approval" if all(t.status in ("Passed", "Waived") for t in doc.tasks) else "Checks Failed"
	doc.checks_run_at = now_datetime()
	doc.save()
	audit.record("period_close.checks_run", doc.doctype, doc.name, {"status": doc.status,
		"failed": [t.check_code for t in doc.tasks if t.status == "Failed"]}, company=doc.company)
	return doc.status


@frappe.whitelist(methods=["POST"])
def waive(name: str, check_code: str, reason: str) -> str:
	frappe.only_for(("Group Controller",))
	if len((reason or "").strip()) < 10:
		frappe.throw(_("Explain the waiver (at least 10 characters)."))
	doc = frappe.get_doc("Period Close Checklist", name)
	waivable = {code: w for code, _d, w, _p in registered_checks()}
	if not waivable.get(check_code):
		frappe.throw(_("Check {0} cannot be waived.").format(check_code))
	task = next((t for t in doc.tasks if t.check_code == check_code), None)
	if not task or task.status != "Failed":
		frappe.throw(_("Only a failed check can be waived."))
	task.status, task.waiver_reason, task.waived_by = "Waived", reason.strip(), frappe.session.user
	if all(t.status in ("Passed", "Waived") for t in doc.tasks):
		doc.status = "Ready For Approval"
	doc.save()
	audit.record("period_close.check_waived", doc.doctype, doc.name, {"check": check_code, "reason": reason},
		company=doc.company)
	return doc.status


def before_submit(doc) -> None:
	if doc.status != "Ready For Approval" or any(t.status not in ("Passed", "Waived") for t in doc.tasks):
		frappe.throw(_("Run the checks and resolve or waive every failure before closing the period."))


def on_submit(doc) -> None:
	period_name = f"{doc.company} {doc.period_start} - {doc.period_end}"
	existing = frappe.db.get_value("Accounting Period", {"company": doc.company, "start_date": doc.period_start,
		"end_date": doc.period_end}, "name")
	if existing:
		period = frappe.get_doc("Accounting Period", existing)
		period.disabled = 0
		for row in period.closed_documents:
			row.closed = 1
		period.flags.ignore_permissions = True
		period.save()
	else:
		# every period-closing document type of the installed apps (ERPNext's own server-side
		# bootstrap mishandles its list of dicts, so the table is filled here)
		period = frappe.get_doc({"doctype": "Accounting Period", "period_name": period_name[:120],
			"company": doc.company, "start_date": doc.period_start, "end_date": doc.period_end,
			"closed_documents": [{"document_type": dt, "closed": 1}
				for dt in dict.fromkeys(frappe.get_hooks("period_closing_doctypes") or [])]})
		period.flags.ignore_permissions = True
		period.insert()
	doc.db_set({"accounting_period": period.name, "status": "Closed"})
	audit.record("period_close.closed", doc.doctype, doc.name, {"accounting_period": period.name,
		"closed_documents": [r.document_type for r in period.closed_documents]}, company=doc.company)


def on_cancel(doc) -> None:
	if doc.accounting_period and frappe.db.exists("Accounting Period", doc.accounting_period):
		frappe.db.set_value("Accounting Period", doc.accounting_period, "disabled", 1)
	doc.db_set("status", "Reopened")
	audit.record("period_close.reopened", doc.doctype, doc.name, {"accounting_period": doc.accounting_period},
		company=doc.company)
