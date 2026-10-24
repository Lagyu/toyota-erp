# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from automotive_core.integration_hub.contracts import INBOUND_MESSAGE_TYPES
from automotive_core.integration_hub.partners import connection_status


class IntegrationPartner(Document):
	def validate(self):
		self.partner_code = (self.partner_code or "").strip().upper()
		unknown = {t.strip() for t in (self.allowed_message_types or "").splitlines() if t.strip()} - set(
			INBOUND_MESSAGE_TYPES
		)
		if unknown:
			frappe.throw(_("Unknown inbound message types: {0}").format(", ".join(sorted(unknown))))
		if self.party_type and not self.party:
			frappe.throw(_("Select the {0} this partner represents.").format(_(self.party_type)))
		if (
			self.service_user
			and self.allowed_message_types
			and not frappe.db.get_value("User", self.service_user, "enabled")
		):
			frappe.throw(_("Service user {0} is disabled.").format(self.service_user))
		if self.allowed_message_types and not self.service_user:
			frappe.throw(_("A service user is required to process inbound messages."))
		if self.adapter == "Local File Drop":
			self.local_directory = f"sites/{frappe.local.site}/private/integration/{self.partner_code}"
		self.connection_status = connection_status(self)
