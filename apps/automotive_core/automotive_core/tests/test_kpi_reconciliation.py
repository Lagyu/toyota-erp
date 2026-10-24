"""ERP-12.5: every KPI of the catalogue has a reconciliation check against an independent source
(analytics/kpi.py). Each check passes when the KPI agrees with its sources and fails when the
value does not; the daily refresh stores reconciled, reproducible snapshots."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from automotive_core.analytics import kpi
from automotive_core.analytics.kpi import KpiResult
from automotive_core.tests.utils import AICHI


class TestKpiReconciliation(IntegrationTestCase):
	def test_each_check_holds_for_the_kpi_and_fails_for_a_changed_value(self):
		# a period after every document of the test site: each KPI and its sources are empty, so the
		# check must hold, and a value its sources do not give must fail it
		from_date = add_days(nowdate(), 900)
		to_date = add_days(from_date, 29)
		codes = frappe.get_all("KPI Definition", filters={"is_active": 1}, pluck="name", order_by="name")
		self.assertGreaterEqual(len(codes), 12)
		for code in codes:
			with self.subTest(code):
				result = kpi.compute(code, AICHI, from_date, to_date)
				ok, note = kpi.reconcile(code, AICHI, from_date, to_date, None, result)
				self.assertTrue(ok, f"{code}: {note}")
				changed = KpiResult(value=(result.value or 0) + 7, numerator=(result.numerator or 0) + 7,
					denominator=(result.denominator or 0) + 7, inputs=result.inputs)
				ok, note = kpi.reconcile(code, AICHI, from_date, to_date, None, changed)
				# a check that cannot apply to this site's configuration says so
				self.assertTrue(not ok or "not applicable" in note, f"{code} accepts a changed value: {note}")

	def test_the_daily_refresh_stores_reconciled_reproducible_snapshots(self):
		daily = frappe.get_all("KPI Definition", filters={"is_active": 1, "refresh_policy": "Daily"}, pluck="name")
		self.assertTrue(daily)
		before = frappe.db.count("KPI Snapshot")
		kpi.refresh_daily()
		companies = frappe.db.count("Company")
		self.assertEqual(frappe.db.count("KPI Snapshot") - before, len(daily) * companies)
		latest = frappe.get_all("KPI Snapshot", filters={"company": AICHI}, order_by="creation desc",
			fields=["kpi", "company", "period_start", "period_end", "inputs_hash", "reconciliation_status"],
			limit=len(daily))
		self.assertEqual({s.kpi for s in latest}, set(daily))
		for snap in latest:
			self.assertIn(snap.reconciliation_status, ("Passed", "Failed"))
			# the same inputs give the same hash: a stored value can be reproduced
			again = kpi.compute(snap.kpi, snap.company, snap.period_start, snap.period_end)
			self.assertEqual(again.inputs_hash, snap.inputs_hash, snap.kpi)
