# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

import datetime as dt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_time

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def slot_minutes(start, end) -> tuple[float, bool]:
	start_t, end_t = get_time(start), get_time(end)
	base = dt.date(2000, 1, 1)
	start_dt = dt.datetime.combine(base, start_t)
	end_dt = dt.datetime.combine(base, end_t)
	crosses = end_dt <= start_dt
	if crosses:
		end_dt += dt.timedelta(days=1)
	return (end_dt - start_dt).total_seconds() / 60.0, crosses


class ShiftPattern(Document):
	def validate(self):
		if not any(self.get(day) for day in WEEKDAYS):
			frappe.throw(_("Select at least one working day."))
		total = 0.0
		codes = set()
		for slot in self.shifts:
			if slot.shift_code in codes:
				frappe.throw(_("Shift code {0} appears twice.").format(slot.shift_code))
			codes.add(slot.shift_code)
			minutes, crosses = slot_minutes(slot.start_time, slot.end_time)
			if slot.break_minutes and slot.break_minutes >= minutes:
				frappe.throw(_("Shift {0}: break is longer than the shift.").format(slot.shift_code))
			slot.crosses_midnight = int(crosses)
			slot.available_minutes = minutes - (slot.break_minutes or 0)
			total += slot.available_minutes
		if total > 24 * 60:
			frappe.throw(_("Shifts add up to more than 24 hours per day."))
		self.available_minutes_per_day = total

	def is_working_day(self, day) -> bool:
		day = frappe.utils.getdate(day)
		if not self.get(WEEKDAYS[day.weekday()]):
			return False
		if self.holiday_list and frappe.db.exists("Holiday", {"parent": self.holiday_list, "holiday_date": day}):
			return False
		return True

	def on_update(self):
		for line in frappe.get_all("Production Line", filters={"shift_pattern": self.name}, pluck="name"):
			frappe.get_doc("Production Line", line).save(ignore_permissions=True)
