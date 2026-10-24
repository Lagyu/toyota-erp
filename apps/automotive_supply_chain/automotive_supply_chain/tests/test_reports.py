"""Supply-chain script reports run through Frappe's report runner."""

from __future__ import annotations

from frappe.desk.query_report import run
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from automotive_core.tests.utils import AICHI
from automotive_manufacturing.tests import catalog


class TestSupplyChainReports(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cat = catalog.build()
		catalog.receive(cls.cat["wheel"], 8, rate=120)

	def test_inventory_position_shows_physical_reserved_and_value(self):
		result = run("Inventory Position", filters={"company": AICHI, "item_code": self.cat["wheel"]})
		rows = [r for r in result["result"] if r["warehouse"] == catalog.LINE_SIDE]
		self.assertTrue(rows)
		row = rows[0]
		self.assertGreaterEqual(row["actual_qty"], 8)
		self.assertEqual(row["available_qty"], row["actual_qty"] - row["reserved_stock"])
		self.assertGreater(row["stock_value"], 0)

	def test_period_reports_run(self):
		period = {"from_date": add_days(nowdate(), -89), "to_date": nowdate()}
		for name in ("Purchase Price Variance", "Supplier Performance"):
			self.assertIsInstance(run(name, filters=period)["result"], list, name)
		self.assertIsInstance(
			run("Intercompany Reconciliation", filters={"to_date": nowdate()})["result"], list
		)
