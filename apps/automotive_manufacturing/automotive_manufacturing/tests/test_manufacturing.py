"""Manufacturing integration tests against real ERPNext documents and ledgers:
configuration, ECO/BOM pinning (J-04), build + genealogy + recall (J-01/J-08),
jidoka hold/andon/disposition (J-05), Kanban pull (J-02 internal), planning (J-03),
and line sequencing."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from automotive_core.governance import approvals
from automotive_core.tests.utils import AICHI, approve, as_user, make_user
from automotive_manufacturing.mes.production import build_entry
from automotive_manufacturing.product_engineering import bom_revisions, configuration
from automotive_manufacturing.quality_control.holds import QualityHoldError
from automotive_manufacturing.tests import catalog
from automotive_manufacturing.traceability import genealogy

ENGINEER = "engineer.mfg@hinode.example"
ENG_MANAGER = "engmgr.mfg@hinode.example"
QM1 = "qm1.mfg@hinode.example"
QM2 = "qm2.mfg@hinode.example"
DIRECTOR = "director.mfg@hinode.example"
PLANNER = "planner.mfg@hinode.example"
SUPERVISOR = "supervisor.mfg@hinode.example"


class ManufacturingTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(ENGINEER, ["Product Engineer", "Manufacturing User"])
		make_user(ENG_MANAGER, ["Engineering Manager", "Manufacturing Manager"])
		make_user(QM1, ["Quality Manager", "Stock User"])
		make_user(QM2, ["Quality Manager", "Stock User"])
		make_user(DIRECTOR, ["Plant Director", "Manufacturing Manager"])
		make_user(PLANNER, ["Production Planner", "Manufacturing User", "Stock User"])
		make_user(SUPERVISOR, ["Production Supervisor", "Manufacturing User"])
		cls.cat = catalog.build()

	def stock_up(self, vehicles: int = 2, foam_batch: str | None = None):
		cat = self.cat
		foam_batch = foam_batch or catalog.make_batch(cat["foam"], f"TFOAM-{frappe.generate_hash(length=6)}",
			"Shiranui Foam Industries")
		catalog.receive(cat["foam"], 3 * vehicles + 3, batch_no=foam_batch, rate=40)
		catalog.receive(cat["wheel"], 4 * vehicles, rate=120)
		ecus = [f"TECU-{frappe.generate_hash(length=8).upper()}" for _ in range(vehicles)]
		catalog.receive(cat["ecu"], vehicles, serial_nos=ecus, rate=900)
		# seats: built from foam into a seat batch at line side
		seat_wo = catalog.work_order(cat["seat"], 2 * vehicles, fg_warehouse=catalog.LINE_SIDE)
		seat_batch = catalog.make_batch(cat["seat"], f"TSEAT-{frappe.generate_hash(length=6)}")
		se = frappe.get_doc(
			__import__("erpnext.manufacturing.doctype.work_order.work_order", fromlist=["make_stock_entry"])
			.make_stock_entry(seat_wo.name, "Manufacture", 2 * vehicles)
		)
		for row in se.items:
			row.use_serial_batch_fields = 1
			if row.is_finished_item:
				row.batch_no = seat_batch
			elif row.item_code == cat["foam"]:
				row.batch_no = foam_batch
		se.insert(ignore_permissions=True)
		se.submit()
		return {"foam_batch": foam_batch, "seat_batch": seat_batch, "ecus": ecus}


class TestConfigurationAndChangeControl(ManufacturingTestCase):
	def test_configuration_rules_and_variant_resolution(self):
		bad = configuration.check_selections(catalog.MODEL, {"Trim": "Base", "Drivetrain": "AWD"})
		self.assertFalse(bad["valid"])
		self.assertIn("AWD needs the Premium trim", [v["message"] for v in bad["violations"]])
		missing = configuration.check_selections(catalog.MODEL, {"Trim": "Premium"})
		self.assertFalse(missing["valid"])
		result = configuration.configure(catalog.MODEL, {"Trim": "Premium", "Drivetrain": "AWD"})
		self.assertEqual(result["variant_item"], self.cat["variants"][("Premium", "AWD")])

	def test_eco_supersedes_and_history_stays_pinned(self):
		cat = self.cat
		variant = cat["variants"][("Premium", "AWD")]
		rev_a = cat["vehicle_boms"][("Premium", "AWD")]
		self.assertEqual(frappe.db.get_value("BOM", rev_a, "automotive_revision"), "A")
		old_wo = catalog.work_order(variant, 1)
		self.assertEqual(old_wo.bom_no, rev_a)
		with as_user(ENGINEER):
			eco = frappe.get_doc({"doctype": "Engineering Change Order", "title": "Five wheels (spare)", "company": AICHI,
				"effective_date": add_days(nowdate(), 1), "changes": [{"change_type": "Change Quantity",
				"target_bom": rev_a, "component": cat["wheel"], "qty": 5}]}).insert()
		from automotive_core.governance.approvals import ApprovalRequiredError

		with as_user(ENGINEER), self.assertRaises(ApprovalRequiredError):
			eco.submit()
		approve(eco, ENGINEER, [ENG_MANAGER, QM1])
		with as_user(ENG_MANAGER):
			eco.reload()
			eco.submit()
		eco.reload()
		self.assertEqual(eco.implementation_status, "Implemented")
		rev_b = eco.revisions[0].new_bom
		self.assertEqual(frappe.db.get_value("BOM", rev_b, "automotive_revision"), "B")
		self.assertEqual(str(frappe.db.get_value("BOM", rev_a, "automotive_effective_to")), str(nowdate()))
		# new order planned after the effective date uses revision B; today's still uses A
		self.assertEqual(catalog.work_order(variant, 1, start_offset_days=2, submit=False).bom_no, rev_b)
		self.assertEqual(catalog.work_order(variant, 1, start_offset_days=0, submit=False).bom_no, rev_a)
		# the submitted historical order is untouched and its BOM cannot be cancelled or re-pointed
		self.assertEqual(frappe.db.get_value("Work Order", old_wo.name, "bom_no"), rev_a)
		with self.assertRaises(bom_revisions.EngineeringControlError):
			frappe.get_doc("BOM", rev_a).cancel()
		with self.assertRaises(bom_revisions.EngineeringControlError):
			frappe.get_doc({"doctype": "BOM Update Log", "update_type": "Replace BOM", "current_bom": rev_a,
				"new_bom": rev_b}).insert(ignore_permissions=True)
		# pinning a BOM outside its effectivity is refused
		with self.assertRaises(bom_revisions.EngineeringControlError):
			catalog.work_order(variant, 1, bom=rev_b, start_offset_days=0, submit=False, pinned=True)


class TestBuildGenealogyAndRecall(ManufacturingTestCase):
	def test_vehicle_build_traceability_and_recall(self):
		cat = self.cat
		stock = self.stock_up(2)
		variant = cat["variants"][("Premium", "AWD")]
		wo = catalog.work_order(variant, 2)
		units = frappe.get_all("Vehicle Unit", filters={"work_order": wo.name}, pluck="name", order_by="build_sequence")
		self.assertEqual(len(units), 2)
		from automotive_manufacturing.engine.vin import is_valid

		self.assertTrue(all(is_valid(v) for v in units))
		for i, vin in enumerate(units):
			build_entry(wo.name, 1, components=[{"item_code": cat["ecu"], "serial_no": stock["ecus"][i]},
				{"item_code": cat["seat"], "batch_no": stock["seat_batch"]}])
			self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "production_status"), "Built")
			self.assertEqual(frappe.db.get_value("Serial No", vin, "warehouse"), catalog.FG)
			self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "quality_status"), "Pending Inspection")
			self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "invoice_status"), "Not Invoiced")
		tree = genealogy.components_of(units[0])
		self.assertIn(stock["ecus"][0], {r["component_serial_no"] for r in tree})
		self.assertIn(stock["foam_batch"], {r["component_batch_no"] for r in tree})  # two levels down
		affected = genealogy.units_containing(cat["foam"], batch_nos=[stock["foam_batch"]])
		self.assertEqual(sorted(a["serial_no"] for a in affected), sorted(units))
		recall = frappe.get_doc({"doctype": "Recall Campaign", "title": "Foam lot", "component_item": cat["foam"],
			"batch_nos": stock["foam_batch"]}).insert()
		from automotive_manufacturing.traceability.recall import analyze

		analyze(recall)
		self.assertEqual(recall.affected_units, 2)
		first_hash = recall.result_sha256
		analyze(recall)
		self.assertEqual(recall.result_sha256, first_hash)  # reproducible
		# exactly-once stock effect: building more than ordered is refused
		with self.assertRaises(frappe.ValidationError):
			build_entry(wo.name, 1)

	def test_unreleased_vehicle_cannot_ship(self):
		cat = self.cat
		self.stock_up(1)
		wo = catalog.work_order(cat["variants"][("Premium", "AWD")], 1)
		build_entry(wo.name, 1)
		vin = frappe.get_all("Vehicle Unit", filters={"work_order": wo.name}, pluck="name")[0]
		dn = frappe.new_doc("Delivery Note")
		dn.company = AICHI
		dn.customer = frappe.db.get_value("Customer", {"is_internal_customer": 1, "represents_company":
			"Hinode Automotive Distribution North America"}, "name")
		dn.append("items", {"item_code": wo.production_item, "qty": 1, "warehouse": catalog.FG,
			"use_serial_batch_fields": 1, "serial_no": vin, "rate": 1000})
		from automotive_manufacturing.quality_control.holds import validate_outbound

		with self.assertRaises(QualityHoldError):
			validate_outbound(dn)


class TestReworkReinspection(ManufacturingTestCase):
	def test_rejected_vehicle_is_released_only_after_reinspection(self):
		from automotive_manufacturing.quality_control import holds
		from automotive_manufacturing.quality_control.inspections import make_reinspection

		self.stock_up(1)
		wo = catalog.work_order(self.cat["variants"][("Premium", "AWD")], 1)
		se = build_entry(wo.name, 1)
		vin = frappe.get_all("Vehicle Unit", filters={"work_order": wo.name}, pluck="name")[0]

		def inspect(status):
			qi = frappe.get_doc({"doctype": "Quality Inspection", "inspection_type": "In Process",
				"automotive_inspection_stage": "Final", "reference_type": "Stock Entry", "reference_name": se.name,
				"item_code": wo.production_item, "item_serial_no": vin, "sample_size": 1, "inspected_by": QM1,
				"status": status, "manual_inspection": 1, "company": AICHI})
			qi.insert(ignore_permissions=True)
			qi.submit()
			return qi

		rejected = inspect("Rejected")
		hold = frappe.db.get_value("Quality Hold", {"serial_no": vin, "status": "Active"}, "name")
		self.assertTrue(hold)
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "quality_status"), "On Hold")
		with self.assertRaises(frappe.ValidationError):  # ERPNext: one inspection per row unless named
			inspect("Accepted")
		with self.assertRaises(frappe.ValidationError):  # only rejected inspections are re-inspected
			make_reinspection(frappe.get_doc("Quality Inspection", rejected.name).amended_from or "MISSING")
		with as_user(QM2):
			again = frappe.get_doc("Quality Inspection", make_reinspection(rejected.name))
			again.submit()
		self.assertEqual(again.child_row_reference, rejected.child_row_reference)
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "quality_status"), "On Hold")  # hold still active
		doc = frappe.get_doc("Quality Hold", hold)
		for check in doc.release_checks:
			check.done = 1
		doc.save(ignore_permissions=True)
		with as_user(QM2):
			holds.release(hold, "Seal replaced; water test passed")
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "quality_status"), "Released")
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "final_inspection"), again.name)
		self.assertEqual(frappe.db.get_value("Quality Inspection", rejected.name, "status"), "Rejected")  # kept


class TestJidoka(ManufacturingTestCase):
	def test_rejected_batch_is_held_until_dispositioned(self):
		cat = self.cat
		batch = catalog.make_batch(cat["wheel"], f"TWHL-{frappe.generate_hash(length=6)}", "Shiranui Foam Industries")
		receipt = catalog.receive(cat["wheel"], 8, warehouse=catalog.PARTS_STORE, batch_no=batch, rate=120)
		qi = frappe.get_doc({"doctype": "Quality Inspection", "inspection_type": "Incoming",
			"automotive_inspection_stage": "Incoming", "reference_type": "Stock Entry", "reference_name": receipt,
			"item_code": cat["wheel"], "batch_no": batch, "sample_size": 2, "inspected_by": "Administrator",
			"status": "Rejected", "manual_inspection": 1, "company": AICHI})
		qi.insert(ignore_permissions=True)
		qi.submit()
		qi.reload()
		hold = qi.automotive_quality_hold
		self.assertEqual(frappe.db.get_value("Quality Hold", hold, "status"), "Active")
		self.assertTrue(qi.automotive_nonconformance)
		move = frappe.get_doc({"doctype": "Stock Entry", "purpose": "Material Transfer",
			"stock_entry_type": "Material Transfer", "company": AICHI,
			"items": [{"item_code": cat["wheel"], "qty": 8, "s_warehouse": catalog.PARTS_STORE,
				"t_warehouse": catalog.LINE_SIDE, "use_serial_batch_fields": 1, "batch_no": batch}]})
		with self.assertRaises(QualityHoldError):
			move.insert(ignore_permissions=True)
		# segregating into quarantine is always allowed
		quarantine = frappe.get_doc({"doctype": "Stock Entry", "purpose": "Material Transfer",
			"stock_entry_type": "Material Transfer", "company": AICHI,
			"items": [{"item_code": cat["wheel"], "qty": 8, "s_warehouse": catalog.PARTS_STORE,
				"t_warehouse": catalog.QUARANTINE, "use_serial_batch_fields": 1, "batch_no": batch}]})
		quarantine.insert(ignore_permissions=True)
		quarantine.submit()
		with as_user(QM1):
			disposition = frappe.get_doc({"doctype": "Quality Disposition", "quality_hold": hold,
				"nonconformance": qi.automotive_nonconformance, "company": AICHI, "decision": "Scrap",
				"item_code": cat["wheel"], "batch_no": batch, "qty": 8, "from_warehouse": catalog.QUARANTINE,
				"justification": "Cracked rims on 2 of 2 samples"}).insert()
		approve(disposition, QM1, [QM2])
		with as_user(QM1):
			disposition.reload()
			disposition.submit()
		disposition.reload()
		self.assertEqual(frappe.db.get_value("Stock Entry", disposition.stock_entry, "automotive_source_name"),
			disposition.name)
		self.assertEqual(frappe.db.get_value("Quality Hold", hold, "status"), "Dispositioned")
		self.assertEqual(flt(frappe.db.get_value("Bin", {"item_code": cat["wheel"], "warehouse": catalog.QUARANTINE},
			"actual_qty")), 0)
		nc = frappe.get_doc("Non Conformance", qi.automotive_nonconformance)
		nc.status = "Resolved"
		with as_user(QM2), self.assertRaises(frappe.ValidationError):
			nc.save(ignore_permissions=True)  # no root cause / CAPA yet

	def test_quality_andon_stops_work_until_qm_restart(self):
		cat = self.cat
		self.stock_up(1)
		wo = catalog.work_order(cat["variants"][("Premium", "AWD")], 1)
		make_user("operator.mfg@hinode.example", ["Line Operator"])
		with as_user("operator.mfg@hinode.example"):
			andon = frappe.get_doc({"doctype": "Andon Event", "production_line": catalog.LINE,
				"workstation": "AICHI-FA1-S30", "reason_code": "Q-DEFECT", "stop_type": "Line Stop",
				"work_order": wo.name, "description": "Loose harness"}).insert()
		andon.reload()
		self.assertTrue(andon.quality_hold)
		with self.assertRaises(QualityHoldError):
			build_entry(wo.name, 1)
		from automotive_manufacturing.quality_control import holds
		from automotive_manufacturing.shop_floor import andon as andon_service

		with as_user(SUPERVISOR):
			andon_service.acknowledge(andon.name)
			andon_service.contain(andon.name, "Re-torqued and 100% check of last 5 units")
		with as_user(QM1), self.assertRaises(frappe.ValidationError):
			andon_service.authorize_restart(andon.name)  # hold still active
		hold = frappe.get_doc("Quality Hold", andon.quality_hold)
		for check in hold.release_checks:
			check.done = 1
		hold.save(ignore_permissions=True)
		with as_user(QM1):
			holds.release(hold.name, "Containment verified")
			self.assertEqual(andon_service.authorize_restart(andon.name, "Restart after check"), "Restarted")
		andon.reload()
		self.assertGreaterEqual(andon.duration_minutes, 0)
		build_entry(wo.name, 1)  # work proceeds after release
		self.assertEqual(frappe.db.get_value("Work Order", wo.name, "produced_qty"), 1)


class TestKanban(ManufacturingTestCase):
	def test_consumption_trigger_and_internal_replenishment(self):
		cat = self.cat
		catalog.receive(cat["wheel"], 40, warehouse=catalog.PARTS_STORE, rate=120)
		loop = frappe.get_doc({"doctype": "Kanban Loop", "loop_code": "KL-TEST-WHEEL", "item_code": cat["wheel"],
			"plant": catalog.PLANT, "production_line": catalog.LINE, "supply_type": "Internal Transfer",
			"line_side_warehouse": catalog.LINE_SIDE, "source_warehouse": catalog.PARTS_STORE, "container_qty": 4,
			"number_of_cards": 3, "lead_time_hours": 2, "daily_demand": 24,
			"delivery_windows": [{"weekday": d, "window_start": "08:00:00", "window_end": "20:00:00"}
				for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")]}).insert(
			ignore_permissions=True)
		self.assertEqual(frappe.db.count("Kanban Card", {"kanban_loop": loop.name, "status": "Full"}), 3)
		stock = self.stock_up(1)  # puts 4 wheels at line side
		catalog.receive(cat["wheel"], 8, rate=120)  # 12 wheels = 3 containers at line side
		wo = catalog.work_order(cat["variants"][("Premium", "AWD")], 1)
		build_entry(wo.name, 1, components=[{"item_code": cat["ecu"], "serial_no": stock["ecus"][0]}])
		# 4 wheels consumed -> 8 left -> only 2 containers -> one card signals
		signals = frappe.get_all("Kanban Signal", filters={"kanban_loop": loop.name}, fields=["name", "status",
			"stock_entry", "signal_source"])
		self.assertEqual(len(signals), 1)
		self.assertEqual(signals[0].signal_source, "Consumption Trigger")
		transfer = frappe.get_doc("Stock Entry", signals[0].stock_entry)
		transfer.submit()
		self.assertEqual(frappe.db.get_value("Kanban Signal", signals[0].name, "status"), "Received")
		self.assertEqual(frappe.db.count("Kanban Card", {"kanban_loop": loop.name, "status": "Full"}), 3)


class TestPlanning(ManufacturingTestCase):
	def test_planning_run_is_reproducible_and_releases_native_documents(self):
		from automotive_manufacturing.production_planning import planning_run

		cat = self.cat
		params = frappe.get_doc({"doctype": "Planning Parameter Set", "parameter_set": "AICHI-TEST",
			"company": AICHI, "plant": catalog.PLANT, "horizon_days": 21, "frozen_zone_days": 0,
			"lot_sizing": "Lot For Lot", "capacity_check": 1, "respect_holidays": 0, "include_forecast": 0})
		params.insert(ignore_permissions=True)
		approve(params, "Administrator", [DIRECTOR])
		params.reload()
		params.submit()
		customer = "Planning Test Dealer"
		if not frappe.db.exists("Customer", customer):
			frappe.get_doc({"doctype": "Customer", "customer_name": customer,
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name")}).insert(ignore_permissions=True)
		so = frappe.get_doc({"doctype": "Sales Order", "company": AICHI, "customer": customer,
			"transaction_date": nowdate(), "delivery_date": add_days(nowdate(), 14),
			"items": [{"item_code": cat["variants"][("Premium", "AWD")], "qty": 3, "rate": 2_500_000,
				"warehouse": catalog.FG, "delivery_date": add_days(nowdate(), 14)}]})
		so.insert(ignore_permissions=True)
		so.submit()
		frappe.flags.automotive_inline_planning = True
		try:
			with as_user(PLANNER):
				run_name = planning_run.start(AICHI, catalog.PLANT, params.name)
			run = frappe.get_doc("Planning Run", run_name)
			self.assertEqual(run.status, "Completed", run.error)
			items = {p.item_code: p for p in run.proposals}
			self.assertEqual(items[cat["variants"][("Premium", "AWD")]].qty, 3)
			self.assertIn(cat["seat"], items)
			self.assertEqual(items[cat["ecu"]].proposal_type, "Purchase")
			with as_user(PLANNER):
				replay_name = planning_run.replay(run_name)
			self.assertEqual(frappe.db.get_value("Planning Run", replay_name, "replay_result"), "Identical")
			with as_user(PLANNER):
				planning_run.decide_all(run_name, "Approved")
				created = planning_run.release(run_name)
		finally:
			frappe.flags.automotive_inline_planning = False
		run.reload()
		self.assertEqual(run.status, "Released")
		self.assertTrue(run.production_plan)
		self.assertTrue(created["Work Order"])
		self.assertTrue(created["Material Request"])
		wo = frappe.get_doc("Work Order", created["Work Order"][0])
		self.assertEqual(wo.automotive_plant, catalog.PLANT)
		self.assertEqual(wo.production_plan, run.production_plan)



class TestSequencing(ManufacturingTestCase):
	def test_daily_schedule_is_leveled_and_changes_are_audited(self):
		from automotive_manufacturing.production_planning import schedule

		cat = self.cat
		for key, count in ((("Premium", "AWD"), 3), (("Base", "FWD"), 3)):
			for _i in range(count):
				catalog.work_order(cat["variants"][key], 1, start_offset_days=1)
		day = add_days(nowdate(), 1)
		name = schedule.build(catalog.LINE, day)
		doc = frappe.get_doc("Plant Schedule", name)
		keys = [s.mix_key for s in doc.slots]
		self.assertEqual(len(keys), 6)
		self.assertNotEqual(keys[:3], [keys[0]] * 3)  # alternating, not batched
		self.assertTrue(all(s.vehicle_unit for s in doc.slots))
		schedule.set_status(name, "Released")
		with self.assertRaises(frappe.ValidationError):
			schedule.resequence(name, [6, 5, 4, 3, 2, 1], "")
		schedule.resequence(name, [6, 5, 4, 3, 2, 1], "Paint shop delay on AWD bodies")
		changes = frappe.get_all("Plant Schedule Change", filters={"plant_schedule": name}, pluck="change_type")
		self.assertEqual(sorted(changes), ["Release", "Resequence"])
