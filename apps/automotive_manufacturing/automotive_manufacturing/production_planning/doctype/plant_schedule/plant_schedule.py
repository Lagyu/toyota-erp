# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document

from automotive_manufacturing.production_planning import schedule


class PlantSchedule(Document):
	def validate(self):
		schedule.validate(self)
