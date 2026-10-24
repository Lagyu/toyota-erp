"""Intercompany order hand-off (ERP-09, J-07): a group company's purchase order addressed
to an internal supplier becomes the seller's sales order.

The seller's sales staff create it, but the source purchase order belongs to the buying
company, which they may not read; ERPNext's mapper needs that read. The mapping therefore
runs as an audited system step on their behalf (governance.elevation) after checking
that the order really is an intercompany order to a company in their scope. The draft
keeps the requester as owner, and they confirm (submit) it with their own rights.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from automotive_core.analytics import scope
from automotive_core.governance import elevation

SELLER_ROLES = ("Sales Manager", "Dealer Liaison", "Group Controller", "System Manager")


def _seller_of(purchase_order: str) -> str:
	po = frappe.db.get_value("Purchase Order", purchase_order, ["supplier", "docstatus"], as_dict=True)
	if not po or po.docstatus != 1:
		frappe.throw(_("Purchase Order {0} is not submitted.").format(purchase_order))
	seller = frappe.db.get_value("Supplier", {"name": po.supplier, "is_internal_supplier": 1}, "represents_company")
	if not seller:
		frappe.throw(_("Purchase Order {0} is not addressed to a group company.").format(purchase_order))
	return seller


@frappe.whitelist(methods=["POST"])
def create_seller_order(purchase_order: str, delivery_date: str | None = None, warehouse: str | None = None) -> str:
	from erpnext.buying.doctype.purchase_order.purchase_order import make_inter_company_sales_order

	frappe.only_for(SELLER_ROLES)
	seller = _seller_of(purchase_order)
	scope.companies({"company": seller})  # the requester must be allowed to act for the seller
	existing = frappe.db.get_value("Sales Order", {"inter_company_order_reference": purchase_order,
		"docstatus": ("<", 2)}, "name")
	if existing:
		return existing
	with elevation.on_behalf_of(_("Seller order for {0}").format(purchase_order), "Purchase Order",
			purchase_order) as requester:
		so = make_inter_company_sales_order(purchase_order)
		if delivery_date:
			so.delivery_date = getdate(delivery_date)
		for item in so.items:
			if warehouse:
				item.warehouse = warehouse
			if delivery_date:
				item.delivery_date = so.delivery_date
		so.insert()
		# Frappe stamps the session user as owner on insert; the order belongs to the requester
		so.db_set("owner", requester, update_modified=False)
	return so.name
