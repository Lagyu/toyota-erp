"""Invariant validation after data generation (requirements §5, §8.1 reconciliation
assertions). Every check reads the ERPNext ledgers directly; results are stored on the
Seed Run and also exposed for the acceptance tests (``automotive_seed.generator.validate.run``).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

from automotive_seed.generator.catalog import AICHI, KYUSHU, NA

COMPANIES = (AICHI, KYUSHU, NA)


def _check(results: list, name: str, ok: bool, detail: Any) -> None:
	results.append({"check": name, "ok": bool(ok), "detail": detail})


def trial_balance(results: list) -> None:
	for company in COMPANIES:
		dr, cr = frappe.db.sql(
			"""select sum(debit), sum(credit) from `tabGL Entry` where company=%s and is_cancelled=0""",
			company,
		)[0]
		_check(
			results,
			f"trial balance {company}",
			abs(flt(dr) - flt(cr)) < 0.01,
			{"debit": flt(dr), "credit": flt(cr)},
		)


def stock_vs_gl(results: list) -> None:
	"""Perpetual inventory: SLE value per company equals the GL balance of its stock accounts."""
	for company in COMPANIES:
		sle = flt(
			frappe.db.sql(
				"""select sum(sle.stock_value_difference) from `tabStock Ledger Entry` sle
			where sle.company=%s and sle.is_cancelled=0""",
				company,
			)[0][0]
		)
		accounts = frappe.db.sql_list(
			"""select distinct ifnull(w.account, c.default_inventory_account)
			from `tabWarehouse` w join `tabCompany` c on c.name = w.company where w.company=%s and w.is_group=0""",
			company,
		)
		accounts = [a for a in accounts if a]
		gl = (
			flt(
				frappe.db.sql(
					"""select sum(debit - credit) from `tabGL Entry` where company=%s and is_cancelled=0
			and account in %s""",
					(company, tuple(accounts) or ("",)),
				)[0][0]
			)
			if accounts
			else 0.0
		)
		tolerance = max(1.0, abs(sle) * 1e-7)
		_check(
			results,
			f"stock value = stock accounts {company}",
			abs(sle - gl) <= tolerance,
			{"stock_ledger": round(sle, 2), "general_ledger": round(gl, 2), "difference": round(sle - gl, 2)},
		)


def bins_vs_ledger(results: list) -> None:
	"""Each Bin equals the balance after the latest stock ledger entry of its item and warehouse
	(reconciliations post balances, not movements, so summing actual_qty would be wrong)."""
	mismatches = frappe.db.sql(
		"""select b.item_code, b.warehouse, b.actual_qty,
			(select sle.qty_after_transaction from `tabStock Ledger Entry` sle
			 where sle.item_code = b.item_code and sle.warehouse = b.warehouse and sle.is_cancelled = 0
			 order by sle.posting_datetime desc, sle.creation desc limit 1) ledger
		from `tabBin` b
		having abs(b.actual_qty - ifnull(ledger, 0)) > 0.0001 limit 20""",
		as_dict=True,
	)
	_check(results, "bin quantity = stock ledger balance", not mismatches, mismatches)
	negative = frappe.db.sql(
		"""select item_code, warehouse, min(qty_after_transaction) q from `tabStock Ledger Entry`
		where is_cancelled = 0 group by item_code, warehouse having q < -0.0001 limit 20""",
		as_dict=True,
	)
	_check(results, "no negative stock", not negative, negative)


def receivables(results: list) -> None:
	from automotive_core.analytics.kpis import days_sales_outstanding, reconcile_receivables

	today = frappe.utils.nowdate()
	for company in COMPANIES:
		result = days_sales_outstanding(company, "2000-01-01", today)
		ok, note = reconcile_receivables(company, "2000-01-01", today, None, result)
		_check(results, f"receivables GL = payment ledger {company}", ok, note)
		ap_gl = flt(
			frappe.db.sql(
				"""select sum(gle.credit - gle.debit) from `tabGL Entry` gle join `tabAccount` a
			on a.name = gle.account where gle.company=%s and gle.is_cancelled=0 and a.account_type='Payable'""",
				company,
			)[0][0]
		)
		# the payment ledger carries invoices positive on both sides (AR and AP)
		ap_ple = flt(
			frappe.db.sql(
				"""select sum(amount) from `tabPayment Ledger Entry` where company=%s
			and account_type='Payable' and delinked=0""",
				company,
			)[0][0]
		)
		_check(
			results,
			f"payables GL = payment ledger {company}",
			abs(ap_gl - ap_ple) <= 1,
			{"gl": round(ap_gl, 2), "payment_ledger": round(ap_ple, 2)},
		)


def intercompany(results: list) -> None:
	from automotive_supply_chain.intercompany.transfer_pricing import reconciliation

	for seller, buyer in ((KYUSHU, AICHI), (AICHI, NA)):
		frappe.set_user("Administrator")
		result = reconciliation(seller, buyer)
		_check(
			results,
			f"intercompany {seller} -> {buyer}",
			result["balanced"],
			{
				k: result[k]
				for k in (
					"invoiced_by_seller",
					"booked_by_buyer",
					"unmatched_sales_invoices",
					"unmatched_purchase_invoices",
					"amount_mismatches",
				)
			},
		)


def traceability(results: list) -> None:
	built = frappe.db.count("Vehicle Unit", {"production_status": "Built"})
	without = frappe.db.sql(
		"""select vu.name from `tabVehicle Unit` vu where vu.production_status = 'Built'
		and not exists (select 1 from `tabGenealogy Link` gl where gl.parent_serial_no = vu.name) limit 10""",
		pluck=True,
	)
	_check(results, "every built vehicle has genealogy", not without, {"built": built, "missing": without})
	serial_mismatch = frappe.db.sql(
		"""select vu.name from `tabVehicle Unit` vu join `tabSerial No` sn on sn.name = vu.name
		where vu.inventory_status in ('In Stock', 'Reserved') and ifnull(sn.warehouse, '') != ifnull(vu.current_warehouse, '')
		limit 10""",
		pluck=True,
	)
	_check(results, "vehicle location = serial warehouse", not serial_mismatch, serial_mismatch)
	unreleased_shipped = frappe.db.sql(
		"""select vu.name from `tabVehicle Unit` vu where vu.inventory_status = 'Delivered'
		and vu.quality_status != 'Released' limit 10""",
		pluck=True,
	)
	_check(results, "no unreleased vehicle delivered", not unreleased_shipped, unreleased_shipped)


def controls(results: list) -> None:
	unapproved = frappe.db.sql(
		"""select po.name from `tabPurchase Order` po where po.docstatus = 1
		and ifnull(po.automotive_release_type, 'Standard') = 'Standard'
		and not exists (select 1 from `tabApproval Request` ar where ar.reference_doctype = 'Purchase Order'
			and ar.reference_name = po.name and ar.status = 'Approved') limit 10""",
		pluck=True,
	)
	_check(results, "every standard purchase order approved", not unapproved, unapproved)
	payments = frappe.db.sql(
		"""select pe.name from `tabPayment Entry` pe where pe.docstatus = 1 and pe.payment_type = 'Pay'
		and not exists (select 1 from `tabApproval Request` ar where ar.reference_doctype = 'Payment Entry'
			and ar.reference_name = pe.name and ar.status = 'Approved') limit 10""",
		pluck=True,
	)
	_check(results, "every supplier payment dual-approved", not payments, payments)
	self_approved = frappe.db.sql(
		"""select ad.name from `tabApproval Decision` ad join `tabApproval Request` ar
		on ar.name = ad.approval_request where ad.decided_by = ar.requested_by limit 10""",
		pluck=True,
	)
	_check(results, "no self-approval", not self_approved, self_approved)
	open_exceptions = frappe.db.sql(
		"""select imx.name from `tabInvoice Match Exception` imx join `tabPurchase Invoice` pi
		on pi.name = imx.purchase_invoice where pi.docstatus = 1 and imx.status in ('Open', 'Rejected') limit 10""",
		pluck=True,
	)
	_check(results, "no submitted invoice with open match exceptions", not open_exceptions, open_exceptions)
	from automotive_core.governance.audit import seal_pending, verify_chain

	while seal_pending():
		pass
	chain = verify_chain()
	_check(results, "audit hash chain intact", chain["ok"], chain)
	from automotive_core.integration_hub.inbound import reconciliation

	frappe.set_user("Administrator")
	inbox = reconciliation()
	_check(results, "inbound messages exactly once", inbox["ok"], inbox)


def targets(results: list, profile: dict) -> None:
	want = profile.get("targets", {})
	have = {
		"internal_users": frappe.db.count(
			"User", {"user_type": "System User", "enabled": 1, "name": ("not in", ("Administrator", "Guest"))}
		),
		"partner_users": frappe.db.count("Partner User Access", {"status": "Active"}),
		"suppliers": frappe.db.count("Supplier", {"supplier_group": "Production Material"}),
		"dealers": frappe.db.count("Customer", {"automotive_dealer_registration": ("is", "set")}),
		"items": frappe.db.count("Item"),
		"boms": frappe.db.count("BOM", {"docstatus": 1}),
		"work_orders": frappe.db.count("Work Order", {"docstatus": 1}),
		"stock_ledger_events": frappe.db.count("Stock Ledger Entry", {"is_cancelled": 0}),
	}
	for key, minimum in want.items():
		_check(results, f"volume {key} >= {minimum}", have.get(key, 0) >= minimum, have.get(key))


def run(ctx=None, state=None) -> dict:
	results: list = []
	for fn in (trial_balance, stock_vs_gl, bins_vs_ledger, receivables, intercompany, traceability, controls):
		try:
			fn(results)
		except Exception as exc:
			_check(results, fn.__name__, False, f"{type(exc).__name__}: {exc}")
	if ctx:
		targets(results, ctx.profile)
	return {"ok": all(r["ok"] for r in results), "checks": results}
