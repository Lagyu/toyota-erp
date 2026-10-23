# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document
from frappe.utils import now_datetime


class TransportException(Document):
	def validate(self):
		if self.is_new() and not self.reported_at:
			self.reported_at = now_datetime()
