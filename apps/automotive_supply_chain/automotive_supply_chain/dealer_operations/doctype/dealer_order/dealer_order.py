# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from automotive_supply_chain.dealer_operations import orders


class DealerOrder(Document):
	def validate(self):
		orders.validate_dealer_order(self)

	def on_submit(self):
		orders.on_dealer_order_submit(self)

	def on_cancel(self):
		if self.sales_order and frappe.db.get_value("Sales Order", self.sales_order, "docstatus") == 1:
			frappe.throw(_("The order was accepted; cancel it through a Dealer Order Change."))
		self.db_set("status", "Cancelled")
