"""Manufacturing script reports run through Frappe's report runner on real documents."""

from __future__ import annotations

import datetime
from unittest.mock import patch

import frappe
from frappe.desk.query_report import run
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_to_date, flt, get_datetime, getdate, nowdate

from automotive_core.tests.utils import AICHI, as_user, make_user
from automotive_manufacturing.mes.production import build_entry
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
		for name, filters in (
			("WIP by Line", {"show_orders": 1}),
			("OEE by Line", {"from_date": add_days(nowdate(), -6), "to_date": nowdate()}),
			("Production Variance", {"from_date": add_days(nowdate(), -29), "to_date": nowdate()}),
		):
			result = run(name, filters=filters)
			self.assertIsInstance(result["result"], list, name)
		lines = {row["production_line"] for row in run("WIP by Line", filters={})["result"]}
		self.assertIn(catalog.LINE, lines)

	def test_traceability_reports_need_a_subject(self):
		self.assertEqual(run("Vehicle Genealogy", filters={})["result"], [])
		self.assertEqual(run("Affected Vehicles", filters={})["result"], [])
		unknown = run(
			"Affected Vehicles", filters={"item_code": self.cat["wheel"], "batch_nos": "NO-SUCH-LOT"}
		)
		self.assertEqual(unknown["result"], [])


OPERATOR = "operator.oee.mfg@hinode.example"
RESPONDER = "supervisor.oee.mfg@hinode.example"


def _quiet_working_day(plant: str, line: str) -> datetime.date:
	"""A working day of the plant long past, with no schedule on the line yet: nothing else in
	the suite builds, stops or schedules on it."""
	from automotive_manufacturing.production_planning.snapshot import working_days

	for back in range(400, 800):
		day = getdate(add_days(nowdate(), -back))
		if working_days(plant, day, day) and not frappe.db.exists(
			"Plant Schedule", {"production_line": line, "schedule_date": day}
		):
			return day
	raise AssertionError("no quiet working day found")


def _fresh_components(bom: str, rates: dict[str, float]) -> tuple[list[dict], float]:
	"""Fresh lots of every component of one unit of the BOM, received at the given rates: the
	components to scan at the build, and the value they are consumed at (batch- and
	serial-valued items: each lot keeps its own rate)."""
	components, value = [], 0.0
	for row in frappe.get_all("BOM Item", filters={"parent": bom}, fields=["item_code", "qty"]):
		tracked = frappe.get_cached_value(
			"Item", row.item_code, ["has_batch_no", "has_serial_no"], as_dict=True
		)
		rate = rates.get(row.item_code, 100.0)
		if tracked.has_serial_no:
			serials = [f"TECU-{frappe.generate_hash(length=8).upper()}" for _ in range(int(row.qty))]
			catalog.receive(row.item_code, row.qty, rate=rate, serial_nos=serials)
			components += [{"item_code": row.item_code, "serial_no": s} for s in serials]
		elif tracked.has_batch_no:
			batch = catalog.make_batch(row.item_code, f"PV-{frappe.generate_hash(length=8)}")
			catalog.receive(row.item_code, row.qty, rate=rate, batch_no=batch)
			components.append({"item_code": row.item_code, "batch_no": batch})
		else:
			raise AssertionError(f"{row.item_code} is valued at the moving average, not per lot")
		value += flt(row.qty) * rate
	return components, value


class TestLineReportValues(IntegrationTestCase):
	"""ERP-04.5, ERP-12.4: OEE inputs, downtime, WIP and production variance computed from
	documents arranged for the test, the reports and the KPIs agreeing."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cat = catalog.build()
		make_user(OPERATOR, ["Line Operator"])
		make_user(RESPONDER, ["Production Supervisor", "Manufacturing User"])
		cls.operator = (
			frappe.db.get_value("Employee", {"user_id": OPERATOR}, "name")
			or frappe.get_doc(
				{
					"doctype": "Employee",
					"first_name": "Tst",
					"last_name": "Operator",
					"gender": "Male",
					"date_of_birth": "1990-04-01",
					"date_of_joining": "2020-04-01",
					"company": AICHI,
					"user_id": OPERATOR,
					"create_user_permission": 0,
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def _andon(self, reason: str, stop_type: str, station: str, raised_at: str) -> str:
		with as_user(OPERATOR):
			name = (
				frappe.get_doc(
					{
						"doctype": "Andon Event",
						"production_line": catalog.LINE,
						"workstation": station,
						"reason_code": reason,
						"stop_type": stop_type,
						"description": "Report value test",
					}
				)
				.insert()
				.name
			)
		frappe.db.set_value("Andon Event", name, "raised_at", raised_at)
		return name

	def _restart(self, name: str, at: str, note: str) -> None:
		"""The supervisor acknowledges at once and authorises the restart at ``at``."""
		from automotive_manufacturing.shop_floor import andon as andon_service

		raised = get_datetime(frappe.db.get_value("Andon Event", name, "raised_at"))
		with as_user(RESPONDER):
			with patch.object(andon_service, "now_datetime", return_value=add_to_date(raised, minutes=1)):
				andon_service.acknowledge(name)
			with patch.object(andon_service, "now_datetime", return_value=get_datetime(at)):
				self.assertEqual(andon_service.authorize_restart(name, note), "Restarted")

	def test_line_stops_book_their_downtime_and_the_oee_of_their_day_counts_them_once(self):
		from automotive_manufacturing.analytics import kpis
		from automotive_manufacturing.production_planning import schedule

		cat = self.cat
		variant = cat["variants"][("Base", "FWD")]
		day = _quiet_working_day(catalog.PLANT, catalog.LINE)
		available = flt(
			frappe.db.get_value("Plant Schedule", schedule.build(catalog.LINE, day), "available_minutes")
		)
		self.assertGreater(available, 0)
		line = frappe.get_doc("Production Line", catalog.LINE)

		# two cords pulled at once: S30 stops the line from 11:00 to 11:45, S20 from 11:40 to 11:55.
		# The restart of the second must not fail on the downtime the first holds on the line's
		# workstation: it is booked on its own station's
		first = self._andon("E-BREAKDOWN", "Line Stop", "AICHI-FA1-S30", f"{day} 11:00:00")
		second = self._andon("E-TOOL", "Line Stop", "AICHI-FA1-S20", f"{day} 11:40:00")
		self._restart(first, f"{day} 11:45:00", "Drive belt replaced")
		self._restart(second, f"{day} 11:55:00", "Fixture re-seated")
		for name, minutes, workstation, note in (
			(first, 45, line.default_workstation, "Drive belt replaced"),
			(second, 15, "AICHI-FA1-S20", "Fixture re-seated"),
		):
			stop = frappe.get_doc("Andon Event", name)
			self.assertEqual(flt(stop.duration_minutes), minutes)
			entry = frappe.get_doc("Downtime Entry", stop.downtime_entry)
			self.assertEqual(entry.workstation, workstation)
			self.assertEqual(entry.operator, self.operator)
			self.assertEqual(
				(get_datetime(entry.from_time), get_datetime(entry.to_time)),
				(get_datetime(stop.raised_at), get_datetime(stop.restarted_at)),
			)
			self.assertEqual(flt(entry.downtime), minutes)
			self.assertIn(stop.name, entry.remarks)
			self.assertIn(note, entry.remarks)
			self.assertEqual(
				(entry.automotive_andon_event, entry.automotive_production_line), (stop.name, catalog.LINE)
			)
			self.assertEqual(entry.stop_reason, "Machine malfunction")  # both equipment stops
		# a call does not stop the line: its minutes stay out of the OEE
		call = self._andon("P-HELP", "Call", "AICHI-FA1-S30", f"{day} 12:00:00")
		frappe.db.set_value("Andon Event", call, "duration_minutes", 20)

		# four units built that day, three of them released at final inspection
		vins = [catalog.released_vehicle(cat, variant) for _ in range(3)]
		for item, qty in ((cat["wheel"], 4), (cat["seat"], 2)):
			catalog.receive(
				item, qty, batch_no=catalog.make_batch(item, f"B-{frappe.generate_hash(length=8)}")
			)
		catalog.receive(
			cat["ecu"], 1, serial_nos=[f"TECU-{frappe.generate_hash(length=8).upper()}"], rate=900
		)
		unreleased = catalog.work_order(variant, 1)
		build_entry(unreleased.name, 1)
		vins += frappe.get_all("Vehicle Unit", filters={"work_order": unreleased.name}, pluck="name")
		self.assertEqual(len(vins), 4)
		for vin in vins:
			frappe.db.set_value("Vehicle Unit", vin, "built_at", f"{day} 10:00:00")

		[row] = run(
			"OEE by Line", filters={"from_date": day, "to_date": day, "production_line": catalog.LINE}
		)["result"]
		running = available - 55  # 11:00 to 11:55, the overlap once
		performance = min(4 * flt(line.takt_time_seconds) / 60.0 / running, 1.0)
		self.assertEqual(
			(row["available_minutes"], row["stop_minutes"], row["units"], row["released"]),
			(available, 55, 4, 3),
		)
		self.assertEqual(row["availability"], round(running / available * 100, 2))
		self.assertEqual(row["performance"], round(performance * 100, 2))
		self.assertEqual(row["quality"], 75.0)
		self.assertEqual(row["oee"], round(running / available * performance * 0.75 * 100, 2))

		# the KPIs of the day: the OEE agrees with the report; the line-stop hours add the two
		# stops' own durations and reconcile with the downtime booked for them
		oee = kpis.oee(AICHI, day, day, catalog.PLANT)
		self.assertEqual(oee.value, row["oee"])
		self.assertTrue(kpis.reconcile_oee(AICHI, day, day, catalog.PLANT, oee)[0])
		hours = kpis.line_stop_hours(AICHI, day, day, catalog.PLANT)
		self.assertEqual((hours.value, hours.denominator), (1.0, 2))
		ok, detail = kpis.reconcile_line_stops(AICHI, day, day, catalog.PLANT, hours)
		self.assertTrue(ok, detail)

	def test_wip_by_line_counts_open_orders_and_values_the_line_side_stock(self):
		cat = self.cat

		def line_row():
			rows = run("WIP by Line", filters={"production_line": catalog.LINE, "show_orders": 1})["result"]
			return rows[0], {r["production_line"]: r for r in rows[1:]}

		before, _orders = line_row()
		batch = catalog.make_batch(cat["wheel"], f"WIP-{frappe.generate_hash(length=8)}")
		catalog.receive(cat["wheel"], 10, rate=111, batch_no=batch)
		after, _orders = line_row()
		self.assertAlmostEqual(after["line_side_value"] - before["line_side_value"], 1110, places=2)

		wo = catalog.work_order(cat["variants"][("Premium", "AWD")], 3)
		catalog.receive(
			cat["seat"], 2, batch_no=catalog.make_batch(cat["seat"], f"WIP-{frappe.generate_hash(length=8)}")
		)
		catalog.receive(
			cat["ecu"], 1, serial_nos=[f"TECU-{frappe.generate_hash(length=8).upper()}"], rate=900
		)
		build_entry(wo.name, 1)
		line, orders = line_row()
		self.assertEqual(
			(orders[wo.name]["planned_qty"], orders[wo.name]["built_qty"], orders[wo.name]["remaining_qty"]),
			(3, 1, 2),
		)
		self.assertEqual(orders[wo.name]["oldest_days"], 0)
		self.assertEqual(line["open_orders"], after["open_orders"] + 1)
		self.assertEqual(line["remaining_qty"], after["remaining_qty"] + 2)
		self.assertEqual(line["remaining_qty"], sum(r["remaining_qty"] for r in orders.values()))
		self.assertEqual(line["open_orders"], len(orders))

	def test_production_variance_sets_the_consumed_material_against_the_bom_standard(self):
		cat = self.cat
		variant = cat["variants"][("Premium", "AWD")]
		wo = catalog.work_order(variant, 2)
		bom = frappe.get_doc("BOM", wo.bom_no)
		rates = {cat["wheel"]: 130, cat["seat"]: 310, cat["ecu"]: 950, cat["foam"]: 45}
		components, consumed = _fresh_components(bom.name, rates)
		build_entry(wo.name, 1, components=components)
		[row] = [
			r
			for r in run("Production Variance", filters={"from_date": nowdate(), "to_date": nowdate()})[
				"result"
			]
			if r["work_order"] == wo.name
		]
		standard = flt(bom.total_cost) / flt(bom.quantity)
		self.assertGreater(standard, 0)
		self.assertEqual((row["planned_qty"], row["produced_qty"], row["qty_variance"]), (2, 1, -1))
		self.assertAlmostEqual(row["standard_cost"], standard, places=2)
		self.assertAlmostEqual(row["actual_cost"], consumed, places=2)
		self.assertAlmostEqual(row["cost_variance"], consumed - standard, places=2)
		self.assertEqual(row["variance_percent"], round((consumed - standard) / standard * 100, 2))
