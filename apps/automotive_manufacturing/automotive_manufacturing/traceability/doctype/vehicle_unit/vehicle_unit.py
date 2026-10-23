# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from automotive_manufacturing.engine import vin

STATE_FIELDS = ("production_status", "quality_status", "inventory_status", "shipment_status", "invoice_status")


class VehicleUnit(Document):
	def validate(self):
		if self.vehicle_model and frappe.get_cached_value("Vehicle Model", self.vehicle_model, "product_family") not in (
			"Powertrain", "Electric Drive"
		) and not vin.is_valid(self.vin):
			frappe.throw(_("{0} is not a valid VIN (check digit).").format(self.vin))
		if not self.is_new() and not self.flags.get("ignore_permissions"):
			before = self.get_doc_before_save()
			for field in STATE_FIELDS:
				if before and before.get(field) != self.get(field):
					raise frappe.PermissionError(_("{0} is maintained by the system from transactions.").format(
						self.meta.get_label(field)))
