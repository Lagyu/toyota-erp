# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime


class TransportBooking(Document):
	def validate(self):
		if get_datetime(self.pickup_window_end) < get_datetime(self.pickup_window_start):
			frappe.throw(_("Pickup window ends before it starts."))
		if get_datetime(self.delivery_window_end) < get_datetime(self.pickup_window_start):
			frappe.throw(_("Delivery deadline is before pickup."))
		if frappe.db.get_value("Supplier", self.carrier, "supplier_group") != "Carriers":
			frappe.throw(_("{0} is not in the Carriers supplier group.").format(self.carrier))
