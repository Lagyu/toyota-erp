"""Manufacturing script reports run through Frappe's report runner on real documents."""

from __future__ import annotations

import frappe
from frappe.desk.query_report import run
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from automotive_manufacturing.tests import catalog


class TestManufacturingReports(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cat = catalog.build()

	def test_where_used_finds_the_vehicle_boms(self):
		result = run("BOM Where Used", filters={"item_code": self.cat["wheel"]})
		parents = {row["parent_item"] for row in result["result"]}
		self.assertTrue(parents & set(self.cat["variants"].values()))
		self.assertTrue(all("path" in row for row in result["result"]))

	def test_line_reports_run(self):
		for name, filters in (("WIP by Line", {"show_orders": 1}),
				("OEE by Line", {"from_date": add_days(nowdate(), -6), "to_date": nowdate()}),
				("Production Variance", {"from_date": add_days(nowdate(), -29), "to_date": nowdate()})):
			result = run(name, filters=filters)
			self.assertIsInstance(result["result"], list, name)
		lines = {row["production_line"] for row in run("WIP by Line", filters={})["result"]}
		self.assertIn(catalog.LINE, lines)

	def test_traceability_reports_need_a_subject(self):
		self.assertEqual(run("Vehicle Genealogy", filters={})["result"], [])
		self.assertEqual(run("Affected Vehicles", filters={})["result"], [])
		unknown = run("Affected Vehicles", filters={"item_code": self.cat["wheel"], "batch_nos": "NO-SUCH-LOT"})
		self.assertEqual(unknown["result"], [])
