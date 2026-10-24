"""Period-close checks contributed by automotive_supply_chain (see
automotive_core.finance_controls.period_close)."""

from __future__ import annotations

import frappe
from frappe import _


def check_match_exceptions(company, period_start, period_end):
	open_ = frappe.db.sql(
		"""select imx.name from `tabInvoice Match Exception` imx
		join `tabPurchase Invoice` pi on pi.name = imx.purchase_invoice
		where pi.company = %s and pi.posting_date between %s and %s and imx.status in ('Open', 'Rejected')
		and pi.docstatus < 2 limit 10""",
		(company, period_start, period_end),
		pluck=True,
	)
	return not open_, ", ".join(open_) or _("none open")


def check_intercompany(company, period_start, period_end):
	from automotive_supply_chain.intercompany.transfer_pricing import reconciliation

	pairs = frappe.db.sql(
		"""select distinct si.company, c.represents_company from `tabSales Invoice` si
		join `tabCustomer` c on c.name = si.customer where si.docstatus = 1 and c.is_internal_customer = 1
		and (si.company = %s or c.represents_company = %s)""",
		(company, company),
	)
	problems = []
	for seller, buyer in pairs:
		result = reconciliation(seller, buyer, str(period_end))
		if not result["balanced"]:
			problems.append(
				f"{seller} -> {buyer}: {len(result['unmatched_sales_invoices'])} unmatched sales, "
				f"{len(result['unmatched_purchase_invoices'])} unmatched purchases, "
				f"{len(result['amount_mismatches'])} amount differences"
			)
	return not problems, "; ".join(problems) or _("{0} pair(s) balanced").format(len(pairs))
