# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt
"""Supplier performance (ERP-05): delivery against the (promised) date, acceptance at
incoming inspection, price variance against order, open discrepancies and how fast
orders are acknowledged — all from receipts, invoices and acknowledgements."""

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate, time_diff_in_hours

from automotive_core.analytics import scope


def execute(filters=None):
	filters = frappe._dict(filters or {})
	to_date = getdate(filters.to_date or nowdate())
	from_date = getdate(filters.from_date or add_days(to_date, -89))
	columns = [
		{
			"fieldname": "supplier",
			"label": _("Supplier"),
			"fieldtype": "Link",
			"options": "Supplier",
			"width": 200,
		},
		{"fieldname": "receipt_lines", "label": _("Receipt Lines"), "fieldtype": "Int", "width": 105},
		{"fieldname": "on_time_percent", "label": _("On Time %"), "fieldtype": "Percent", "width": 95},
		{"fieldname": "received_qty", "label": _("Received Qty"), "fieldtype": "Float", "width": 110},
		{"fieldname": "rejected_qty", "label": _("Rejected Qty"), "fieldtype": "Float", "width": 105},
		{"fieldname": "reject_ppm", "label": _("Reject PPM"), "fieldtype": "Float", "width": 100},
		{
			"fieldname": "price_variance",
			"label": _("Price Variance vs Order"),
			"fieldtype": "Currency",
			"width": 170,
		},
		{
			"fieldname": "open_discrepancies",
			"label": _("Open Discrepancies"),
			"fieldtype": "Int",
			"width": 140,
		},
		{"fieldname": "ack_hours", "label": _("Avg Acknowledgement (h)"), "fieldtype": "Float", "width": 170},
	]
	companies = scope.companies(filters)
	values = {"companies": scope.sql_in(companies), "from": from_date, "to": to_date}
	supplier_clause = ""
	if filters.supplier:
		supplier_clause = " and pr.supplier = %(supplier)s"
		values["supplier"] = filters.supplier
	stats: dict[str, dict] = {}
	for row in frappe.db.sql(
		f"""select pr.supplier, pr.posting_date, ifnull(poi.automotive_promised_date,
			poi.schedule_date) due, pri.qty, pri.rejected_qty
		from `tabPurchase Receipt Item` pri join `tabPurchase Receipt` pr on pr.name = pri.parent
		join `tabPurchase Order Item` poi on poi.name = pri.purchase_order_item
		join `tabSupplier` s on s.name = pr.supplier
		where pr.docstatus = 1 and pr.is_return = 0 and s.is_internal_supplier = 0 and pr.company in %(companies)s
		and pr.posting_date between %(from)s and %(to)s {supplier_clause}""",  # noqa: S608 (constant SQL fragments; values are bound)
		values,
		as_dict=True,
	):
		s = stats.setdefault(row.supplier, {"lines": 0, "on_time": 0, "qty": 0.0, "rejected": 0.0})
		s["lines"] += 1
		s["on_time"] += int(row.posting_date <= row.due)
		s["qty"] += flt(row.qty) + flt(row.rejected_qty)
		s["rejected"] += flt(row.rejected_qty)
	variance = dict(
		frappe.db.sql(
			f"""select pi.supplier, sum((pii.base_net_rate - poi.base_net_rate) * pii.qty)
		from `tabPurchase Invoice Item` pii join `tabPurchase Invoice` pi on pi.name = pii.parent
		join `tabPurchase Order Item` poi on poi.name = pii.po_detail
		where pi.docstatus = 1 and pi.is_return = 0 and pi.company in %(companies)s
		and pi.posting_date between %(from)s and %(to)s {supplier_clause.replace("pr.", "pi.")}
		group by pi.supplier""",  # noqa: S608 (constant SQL fragments; values are bound)
			values,
		)
	)
	data = []
	for supplier in sorted(stats):
		s = stats[supplier]
		acks = frappe.db.sql(
			"""select po.creation, ack.acknowledged_at from `tabSupplier Order Acknowledgement` ack
			join `tabPurchase Order` po on po.name = ack.purchase_order where ack.docstatus = 1 and ack.supplier = %s
			and po.company in %s and date(ack.acknowledged_at) between %s and %s""",
			(supplier, scope.sql_in(companies), from_date, to_date),
			as_dict=True,
		)
		hours = [time_diff_in_hours(a.acknowledged_at, a.creation) for a in acks if a.acknowledged_at]
		data.append(
			{
				"supplier": supplier,
				"receipt_lines": s["lines"],
				"on_time_percent": round(s["on_time"] / s["lines"] * 100, 2) if s["lines"] else None,
				"received_qty": s["qty"],
				"rejected_qty": s["rejected"],
				"reject_ppm": round(s["rejected"] / s["qty"] * 1_000_000, 1) if s["qty"] else 0,
				"price_variance": flt(variance.get(supplier)),
				"open_discrepancies": frappe.db.count(
					"Receipt Discrepancy", {"supplier": supplier, "status": "Open"}
				),
				"ack_hours": round(sum(hours) / len(hours), 1) if hours else None,
			}
		)
	return columns, data
