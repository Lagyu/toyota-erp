# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from automotive_manufacturing.shop_floor import handover


class ShiftHandover(Document):
	def validate(self):
		if self.is_new():
			self.outgoing_supervisor = frappe.session.user
		if self.incoming_supervisor == self.outgoing_supervisor:
			frappe.throw(_("The incoming supervisor must be a different person."))
		self.status = "Draft"

	def before_submit(self):
		handover.compute(self)

	def on_submit(self):
		self.db_set("status", "Handed Over")
