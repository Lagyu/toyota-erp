# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe.model.document import Document

from automotive_supply_chain.supplier_collaboration import shipments


class AdvanceShippingNotice(Document):
	def validate(self):
		shipments.validate_asn(self)

	def on_submit(self):
		shipments.on_asn_submit(self)

	def on_cancel(self):
		if frappe.db.exists("Purchase Receipt", {"automotive_asn": self.name, "docstatus": 1}):
			frappe.throw(frappe._("Goods were already received against this ASN."))
		self.db_set("status", "Cancelled")
