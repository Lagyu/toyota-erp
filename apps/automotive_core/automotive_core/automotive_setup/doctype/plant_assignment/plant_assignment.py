# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from automotive_core.automotive_setup.access_sync import effective_status, on_plant_assignment_change


class PlantAssignment(Document):
	def validate(self):
		if self.to_date and getdate(self.to_date) < getdate(self.from_date):
			frappe.throw(_("To Date must be on or after From Date."))
		if (
			self.production_line
			and frappe.db.get_value("Production Line", self.production_line, "plant") != self.plant
		):
			frappe.throw(
				_("Production line {0} is not in plant {1}.").format(self.production_line, self.plant)
			)
		overlapping = frappe.db.sql(
			"""select name from `tabPlant Assignment`
			where employee=%(employee)s and plant=%(plant)s and name!=%(name)s
			and from_date <= %(to)s and ifnull(to_date, '9999-12-31') >= %(from)s""",
			{
				"employee": self.employee,
				"plant": self.plant,
				"name": self.name or "",
				"from": self.from_date,
				"to": self.to_date or "9999-12-31",
			},
		)
		if overlapping:
			frappe.throw(
				_("{0} already has an overlapping assignment to {1} ({2}).").format(
					self.employee_name or self.employee, self.plant, overlapping[0][0]
				)
			)
		self.status = effective_status(self.from_date, self.to_date)

	def on_update(self):
		on_plant_assignment_change(self)

	def on_trash(self):
		self.status = "Ended"
		self.db_set("status", "Ended", update_modified=False)
		on_plant_assignment_change(self)
