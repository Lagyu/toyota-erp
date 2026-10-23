# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_core.automotive_setup.access_sync import (
	effective_status,
	on_partner_access_change,
	validate_partner_access,
)
from automotive_core.governance import audit


class PartnerUserAccess(Document):
	def validate(self):
		validate_partner_access(self)
		if self.party_type == "Supplier":
			self.customer = None
		else:
			self.supplier = None
		self.status = effective_status(self.from_date, self.to_date, revoked=self.status == "Revoked")

	def on_update(self):
		on_partner_access_change(self)
		audit.record("access.partner." + self.status.lower(), self.doctype, self.name,
			{"user": self.user, "party_type": self.party_type, "party": self.supplier or self.customer})

	def revoke(self):
		self.status = "Revoked"
		self.save()

	def on_trash(self):
		self.status = "Revoked"
		on_partner_access_change(self)
