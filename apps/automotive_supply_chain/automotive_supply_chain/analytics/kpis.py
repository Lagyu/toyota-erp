"""Supply-chain KPIs (ERP-12): OTIF, supplier on-time delivery, rejected material,
stock accuracy and purchase-to-pay cycle time."""

from __future__ import annotations

import frappe
from frappe.utils import date_diff, flt

from automotive_core.analytics.kpi import KpiResult, ratio

DEFINITIONS = [
	{
		"kpi_code": "SC-OTIF",
		"kpi_name": "Dealer OTIF (on time in full)",
		"domain": "Delivery",
		"unit": "Percent",
		"direction": "Higher Is Better",
		"target_value": 95,
		"refresh_policy": "Daily",
		"compute_method": "automotive_supply_chain.analytics.kpis.otif",
		"reconciliation_method": "automotive_supply_chain.analytics.kpis.reconcile_otif",
		"owner_role": "Sales Manager",
		"formula": "Sales order lines fully delivered on or before their promised/delivery date / lines due in period",
		"source_doctypes": "Sales Order Item, Delivery Note Item",
		"authorization_filter": "Company User Permission of the caller.",
		"reconciliation_check": "Delivered quantity of due lines equals the Delivery Note quantity booked against them, "
		"and the lines on time and in full, counted again from the Delivery Notes, equal the KPI's.",
	},
	{
		"kpi_code": "SC-SUP-OTD",
		"kpi_name": "Supplier On-Time Delivery",
		"domain": "Procurement",
		"unit": "Percent",
		"direction": "Higher Is Better",
		"target_value": 97,
		"refresh_policy": "Daily",
		"compute_method": "automotive_supply_chain.analytics.kpis.supplier_otd",
		"reconciliation_method": "automotive_supply_chain.analytics.kpis.reconcile_supplier_otd",
		"owner_role": "Procurement Manager",
		"formula": "Receipt lines received on/before the promised (or scheduled) date / receipt lines in period",
		"source_doctypes": "Purchase Receipt Item, Purchase Order Item",
		"authorization_filter": "Company User Permission; supplier portal users see only their own score.",
		"reconciliation_check": "Lines counted equal submitted receipt lines linked to purchase orders in period.",
	},
	{
		"kpi_code": "SC-REJECT",
		"kpi_name": "Rejected Material Rate",
		"domain": "Quality",
		"unit": "Percent",
		"direction": "Lower Is Better",
		"target_value": 0.5,
		"refresh_policy": "Daily",
		"compute_method": "automotive_supply_chain.analytics.kpis.rejected_rate",
		"reconciliation_method": "automotive_supply_chain.analytics.kpis.reconcile_rejected_rate",
		"owner_role": "Quality Manager",
		"formula": "Rejected receipt quantity / (accepted + rejected) receipt quantity x 100",
		"source_doctypes": "Purchase Receipt Item",
		"authorization_filter": "Company User Permission of the caller.",
		"reconciliation_check": "Rejected quantity equals stock received into rejected warehouses by those receipts.",
	},
	{
		"kpi_code": "SC-STOCK-ACC",
		"kpi_name": "Stock Accuracy (cycle counts)",
		"domain": "Inventory",
		"unit": "Percent",
		"direction": "Higher Is Better",
		"target_value": 99,
		"refresh_policy": "Daily",
		"compute_method": "automotive_supply_chain.analytics.kpis.stock_accuracy",
		"reconciliation_method": "automotive_supply_chain.analytics.kpis.reconcile_stock_accuracy",
		"owner_role": "Group Controller",
		"formula": "Counted lines within tolerance / counted lines in posted cycle counts x 100",
		"source_doctypes": "Cycle Count, Cycle Count Line, Stock Reconciliation",
		"authorization_filter": "Company User Permission of the caller.",
		"reconciliation_check": "Every count with variances has a submitted Stock Reconciliation, and the count lines "
		"within tolerance equal the KPI's numerator.",
	},
	{
		"kpi_code": "SC-P2P",
		"kpi_name": "Purchase-to-Pay Cycle (days)",
		"domain": "Procurement",
		"unit": "Days",
		"direction": "Lower Is Better",
		"target_value": 45,
		"refresh_policy": "Daily",
		"compute_method": "automotive_supply_chain.analytics.kpis.p2p_cycle",
		"reconciliation_method": "automotive_supply_chain.analytics.kpis.reconcile_p2p",
		"owner_role": "Group Controller",
		"formula": "Average days from purchase order date to the payment date of the invoice paid in period",
		"source_doctypes": "Payment Entry Reference, Purchase Invoice Item, Purchase Order",
		"authorization_filter": "Company User Permission of the caller.",
		"reconciliation_check": "Invoices counted are fully paid (outstanding 0) by payments in period, and the days "
		"recomputed from the Payment Ledger equal the KPI's.",
	},
]


def _lines_due(company, from_date, to_date):
	return frappe.db.sql(
		"""select soi.name, soi.stock_qty, soi.delivered_qty * soi.conversion_factor delivered,
			ifnull(so.automotive_promised_date, soi.delivery_date) due,
			(select max(dn.posting_date) from `tabDelivery Note Item` dni join `tabDelivery Note` dn on dn.name = dni.parent
			 where dni.so_detail = soi.name and dn.docstatus = 1 and dn.is_return = 0) last_delivery
		from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
		join `tabCustomer` c on c.name = so.customer
		where so.docstatus = 1 and so.company = %s and c.is_internal_customer = 0
		and ifnull(so.automotive_promised_date, soi.delivery_date) between %s and %s""",
		(company, from_date, to_date), as_dict=True)


def otif(company, from_date, to_date, plant=None) -> KpiResult:
	lines = _lines_due(company, from_date, to_date)
	good = sum(1 for ln in lines if flt(ln.delivered) >= flt(ln.stock_qty) and ln.last_delivery and ln.last_delivery <= ln.due)
	return KpiResult(ratio(good, len(lines)), good, len(lines), {"lines": [ln.name for ln in lines]})


def reconcile_otif(company, from_date, to_date, plant, result) -> tuple[bool, str]:
	names = result.inputs.get("lines") or [""]
	so_delivered = flt(frappe.db.sql("select sum(delivered_qty) from `tabSales Order Item` where name in %s",
		[tuple(names)])[0][0])
	dn_qty = flt(frappe.db.sql("""select sum(dni.qty) from `tabDelivery Note Item` dni join `tabDelivery Note` dn
		on dn.name = dni.parent where dn.docstatus = 1 and dni.so_detail in %s""", [tuple(names)])[0][0])
	# the lines on time and in full, counted again from the delivery notes themselves (returns net
	# out of the quantity, as they do on the order; the last delivery is the last outbound one)
	rows = frappe.db.sql("""select soi.stock_qty, ifnull(so.automotive_promised_date, soi.delivery_date) due,
			sum(case when dn.name is null then 0 else dni.stock_qty end) shipped,
			max(case when dn.is_return = 0 then dn.posting_date end) last_delivery
		from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
		left join `tabDelivery Note Item` dni on dni.so_detail = soi.name
		left join `tabDelivery Note` dn on dn.name = dni.parent and dn.docstatus = 1
		where soi.name in %s group by soi.name, soi.stock_qty, due""", [tuple(names)], as_dict=True)
	good = sum(1 for r in rows if flt(r.shipped) >= flt(r.stock_qty) and r.last_delivery and r.last_delivery <= r.due)
	counted = len([n for n in names if n])
	ok = abs(so_delivered - dn_qty) < 1e-6 and good == int(flt(result.numerator)) and counted == int(flt(result.denominator))
	return ok, f"SO delivered {so_delivered} vs DN qty {dn_qty}; on time in full {good} of {counted} by the delivery notes"


def _receipt_lines(company, from_date, to_date):
	return frappe.db.sql(
		"""select pri.name, pr.posting_date, ifnull(poi.automotive_promised_date, poi.schedule_date) due,
			pri.qty, pri.rejected_qty
		from `tabPurchase Receipt Item` pri join `tabPurchase Receipt` pr on pr.name = pri.parent
		join `tabPurchase Order Item` poi on poi.name = pri.purchase_order_item
		where pr.docstatus = 1 and pr.is_return = 0 and pr.company = %s and pr.posting_date between %s and %s""",
		(company, from_date, to_date), as_dict=True)


def supplier_otd(company, from_date, to_date, plant=None) -> KpiResult:
	lines = _receipt_lines(company, from_date, to_date)
	on_time = sum(1 for ln in lines if ln.posting_date <= ln.due)
	return KpiResult(ratio(on_time, len(lines)), on_time, len(lines), {"count": len(lines)})


def reconcile_supplier_otd(company, from_date, to_date, plant, result) -> tuple[bool, str]:
	count = frappe.db.sql("""select count(*) from `tabPurchase Receipt Item` pri join `tabPurchase Receipt` pr
		on pr.name = pri.parent where pr.docstatus = 1 and pr.is_return = 0 and pr.company = %s
		and pr.posting_date between %s and %s and ifnull(pri.purchase_order_item, '') != ''""",
		(company, from_date, to_date))[0][0]
	return int(count) == int(result.denominator or 0), f"{count} receipt lines vs {result.denominator} counted"


def rejected_rate(company, from_date, to_date, plant=None) -> KpiResult:
	row = frappe.db.sql("""select sum(pri.qty), sum(pri.rejected_qty) from `tabPurchase Receipt Item` pri
		join `tabPurchase Receipt` pr on pr.name = pri.parent where pr.docstatus = 1 and pr.is_return = 0
		and pr.company = %s and pr.posting_date between %s and %s""", (company, from_date, to_date))[0]
	accepted, rejected = flt(row[0]), flt(row[1])
	return KpiResult(ratio(rejected, accepted + rejected), rejected, accepted + rejected, {})


def reconcile_rejected_rate(company, from_date, to_date, plant, result) -> tuple[bool, str]:
	sle = flt(frappe.db.sql("""select sum(sle.actual_qty) from `tabStock Ledger Entry` sle
		join `tabPurchase Receipt` pr on pr.name = sle.voucher_no and sle.voucher_type = 'Purchase Receipt'
		join `tabPurchase Receipt Item` pri on pri.name = sle.voucher_detail_no
		where sle.is_cancelled = 0 and pr.is_return = 0 and pr.company = %s and pr.posting_date between %s and %s
		and sle.warehouse = pri.rejected_warehouse""", (company, from_date, to_date))[0][0])
	return abs(sle - flt(result.numerator)) < 1e-6, f"rejected qty {flt(result.numerator)} vs rejected-warehouse SLE {sle}"


def stock_accuracy(company, from_date, to_date, plant=None) -> KpiResult:
	row = frappe.db.sql("""select sum(lines_within_tolerance), sum(lines_counted) from `tabCycle Count`
		where docstatus = 1 and company = %s and count_date between %s and %s""", (company, from_date, to_date))[0]
	return KpiResult(ratio(flt(row[0]), flt(row[1])), flt(row[0]), flt(row[1]), {})


def reconcile_stock_accuracy(company, from_date, to_date, plant, result) -> tuple[bool, str]:
	missing = frappe.db.sql("""select cc.name from `tabCycle Count` cc where cc.docstatus = 1 and cc.company = %s
		and cc.count_date between %s and %s and cc.lines_within_tolerance < cc.lines_counted
		and ifnull(cc.stock_reconciliation, '') = ''""", (company, from_date, to_date))
	# the lines within tolerance, counted on the count lines rather than the counts' totals
	within = flt(frappe.db.sql("""select count(*) from `tabCycle Count Line` ccl join `tabCycle Count` cc
		on cc.name = ccl.parent where cc.docstatus = 1 and cc.company = %s and cc.count_date between %s and %s
		and ccl.within_tolerance = 1""", (company, from_date, to_date))[0][0])
	ok = not missing and int(within) == int(flt(result.numerator))
	return ok, f"{len(missing)} count(s) with variances but no stock reconciliation; {int(within)} lines within tolerance"


def p2p_cycle(company, from_date, to_date, plant=None) -> KpiResult:
	rows = frappe.db.sql("""select distinct pi.name, min(po.transaction_date) po_date, max(pe.posting_date) paid
		from `tabPayment Entry Reference` per join `tabPayment Entry` pe on pe.name = per.parent
		join `tabPurchase Invoice` pi on pi.name = per.reference_name and per.reference_doctype = 'Purchase Invoice'
		join `tabPurchase Invoice Item` pii on pii.parent = pi.name
		join `tabPurchase Order` po on po.name = pii.purchase_order
		where pe.docstatus = 1 and pe.company = %s and pe.posting_date between %s and %s and pi.outstanding_amount <= 0
		group by pi.name""", (company, from_date, to_date), as_dict=True)
	days = [date_diff(r.paid, r.po_date) for r in rows]
	value = round(sum(days) / len(days), 2) if days else None
	return KpiResult(value, sum(days), len(days), {"invoices": [r.name for r in rows]})


def reconcile_p2p(company, from_date, to_date, plant, result) -> tuple[bool, str]:
	names = result.inputs.get("invoices") or [""]
	open_ = frappe.db.sql("select count(*) from `tabPurchase Invoice` where name in %s and outstanding_amount > 0",
		[tuple(names)])[0][0]
	# the days again, with the payment dates from the payment ledger
	rows = frappe.db.sql("""select ple.against_voucher_no invoice, max(ple.posting_date) paid,
			(select min(po.transaction_date) from `tabPurchase Invoice Item` pii join `tabPurchase Order` po
			 on po.name = pii.purchase_order where pii.parent = ple.against_voucher_no) ordered
		from `tabPayment Ledger Entry` ple
		where ple.against_voucher_type = 'Purchase Invoice' and ple.against_voucher_no in %s and ple.delinked = 0
		and ple.voucher_type = 'Payment Entry' and ple.posting_date between %s and %s
		group by ple.against_voucher_no""", [tuple(names), from_date, to_date], as_dict=True)
	days = sum(date_diff(r.paid, r.ordered) for r in rows if r.paid and r.ordered)
	counted = len([n for n in names if n])
	ok = int(open_) == 0 and counted == int(flt(result.denominator)) and abs(days - flt(result.numerator)) < 1e-6
	return ok, f"{open_} counted invoice(s) still outstanding; {days} days over {counted} invoice(s) by the payment ledger"
