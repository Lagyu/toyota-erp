"""Intercompany document hand-offs (ERP-09, J-07).

* a group company's purchase order to an internal supplier -> the seller's sales order
* the seller's delivery note to an internal customer -> the buyer's purchase receipt

Each side's staff act only for their own company, but ERPNext's intercompany mappers
read the other company's source document, which row-level scope (and, for sales staff,
role rights) does not allow. The mapping therefore runs as an audited system step on the
requester's behalf (governance.elevation) after checking that the source really is an
intercompany document addressed to a company in their scope. The draft keeps the
requester as owner, and they confirm (submit) it with their own rights.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from automotive_core.analytics import scope
from automotive_core.governance import elevation

SELLER_ROLES = ("Sales Manager", "Dealer Liaison", "Group Controller", "System Manager")
RECEIVING_ROLES = ("Warehouse Clerk", "Logistics Coordinator", "Stock Manager", "System Manager")


def _seller_of(purchase_order: str) -> str:
	po = frappe.db.get_value("Purchase Order", purchase_order, ["supplier", "docstatus"], as_dict=True)
	if not po or po.docstatus != 1:
		frappe.throw(_("Purchase Order {0} is not submitted.").format(purchase_order))
	seller = frappe.db.get_value(
		"Supplier", {"name": po.supplier, "is_internal_supplier": 1}, "represents_company"
	)
	if not seller:
		frappe.throw(_("Purchase Order {0} is not addressed to a group company.").format(purchase_order))
	return seller


@frappe.whitelist(methods=["POST"])
def create_seller_order(
	purchase_order: str, delivery_date: str | None = None, warehouse: str | None = None
) -> str:
	from erpnext.buying.doctype.purchase_order.purchase_order import make_inter_company_sales_order

	frappe.only_for(SELLER_ROLES)
	seller = _seller_of(purchase_order)
	scope.companies({"company": seller})  # the requester must be allowed to act for the seller
	existing = frappe.db.get_value(
		"Sales Order", {"inter_company_order_reference": purchase_order, "docstatus": ("<", 2)}, "name"
	)
	if existing:
		return existing
	with elevation.on_behalf_of(
		_("Seller order for {0}").format(purchase_order), "Purchase Order", purchase_order
	) as requester:
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


def _buyer_of(delivery_note: str) -> str:
	dn = frappe.db.get_value(
		"Delivery Note", delivery_note, ["customer", "docstatus", "is_return"], as_dict=True
	)
	if not dn or dn.docstatus != 1 or dn.is_return:
		frappe.throw(_("Delivery Note {0} is not a submitted shipment.").format(delivery_note))
	buyer = frappe.db.get_value(
		"Customer", {"name": dn.customer, "is_internal_customer": 1}, "represents_company"
	)
	if not buyer:
		frappe.throw(_("Delivery Note {0} is not addressed to a group company.").format(delivery_note))
	return buyer


@frappe.whitelist(methods=["POST"])
def create_buyer_receipt(delivery_note: str, warehouse: str | None = None) -> str:
	from erpnext.stock.doctype.delivery_note.delivery_note import make_inter_company_purchase_receipt

	frappe.only_for(RECEIVING_ROLES)
	buyer = _buyer_of(delivery_note)
	scope.companies({"company": buyer})  # the requester must be allowed to act for the buyer
	existing = frappe.db.get_value(
		"Purchase Receipt", {"inter_company_reference": delivery_note, "docstatus": ("<", 2)}, "name"
	)
	if existing:
		return existing
	if warehouse and frappe.db.get_value("Warehouse", warehouse, "company") != buyer:
		frappe.throw(_("{0} is not a warehouse of {1}.").format(warehouse, buyer))
	with elevation.on_behalf_of(
		_("Receipt for {0}").format(delivery_note), "Delivery Note", delivery_note
	) as requester:
		pr = make_inter_company_purchase_receipt(delivery_note)
		for item in pr.items:
			if warehouse:
				item.warehouse = warehouse
		pr.insert()
		pr.db_set("owner", requester, update_modified=False)
	return pr.name
