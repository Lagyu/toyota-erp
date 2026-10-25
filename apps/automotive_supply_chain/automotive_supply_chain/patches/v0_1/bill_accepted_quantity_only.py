"""Sites set up before the tenant turned it off bill the accepted quantity only: an invoice
made from a receipt with rejected goods billed them too (ERPNext's default), which the
three-way match refuses, so it could not be submitted."""

import frappe


def execute():
	if frappe.db.get_single_value("Buying Settings", "bill_for_rejected_quantity_in_purchase_invoice"):
		frappe.db.set_single_value("Buying Settings", "bill_for_rejected_quantity_in_purchase_invoice", 0)
