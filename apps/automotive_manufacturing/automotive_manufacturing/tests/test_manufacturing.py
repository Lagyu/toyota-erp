"""Manufacturing integration tests against real ERPNext documents and ledgers:
configuration, ECO/BOM pinning (J-04), build + genealogy + recall (J-01/J-08),
MES production events through the integration hub, jidoka hold/andon/disposition (J-05),
Kanban pull and stockout escalation (J-02 internal), planning (J-03), and line sequencing."""

from __future__ import annotations

import datetime
import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, add_to_date, cint, flt, getdate, now_datetime, nowdate

from automotive_core.governance import approvals
from automotive_core.integration_hub import inbound
from automotive_core.tests.utils import AICHI, approve, as_user, make_user
from automotive_core.utils import hook_dict
from automotive_manufacturing.mes.production import build_entry
from automotive_manufacturing.product_engineering import bom_revisions, configuration
from automotive_manufacturing.production_planning import snapshot
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
		foam_batch = foam_batch or catalog.make_batch(
			cat["foam"], f"TFOAM-{frappe.generate_hash(length=6)}", "Shiranui Foam Industries"
		)
		catalog.receive(cat["foam"], 3 * vehicles + 3, batch_no=foam_batch, rate=40)
		catalog.receive(cat["wheel"], 4 * vehicles, rate=120)
		ecus = [f"TECU-{frappe.generate_hash(length=8).upper()}" for _ in range(vehicles)]
		catalog.receive(cat["ecu"], vehicles, serial_nos=ecus, rate=900)
		# seats: built from foam into a seat batch at line side
		seat_wo = catalog.work_order(cat["seat"], 2 * vehicles, fg_warehouse=catalog.LINE_SIDE)
		seat_batch = catalog.make_batch(cat["seat"], f"TSEAT-{frappe.generate_hash(length=6)}")
		se = frappe.get_doc(
			__import__(
				"erpnext.manufacturing.doctype.work_order.work_order", fromlist=["make_stock_entry"]
			).make_stock_entry(seat_wo.name, "Manufacture", 2 * vehicles)
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
			eco = frappe.get_doc(
				{
					"doctype": "Engineering Change Order",
					"title": "Five wheels (spare)",
					"company": AICHI,
					"effective_date": add_days(nowdate(), 1),
					"changes": [
						{
							"change_type": "Change Quantity",
							"target_bom": rev_a,
							"component": cat["wheel"],
							"qty": 5,
						}
					],
				}
			).insert()
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
			frappe.get_doc(
				{
					"doctype": "BOM Update Log",
					"update_type": "Replace BOM",
					"current_bom": rev_a,
					"new_bom": rev_b,
				}
			).insert(ignore_permissions=True)
		# pinning a BOM outside its effectivity is refused
		with self.assertRaises(bom_revisions.EngineeringControlError):
			catalog.work_order(variant, 1, bom=rev_b, start_offset_days=0, submit=False, pinned=True)

	def test_change_request_becomes_an_eco_the_engineer_completes(self):
		cat = self.cat
		bom = cat["vehicle_boms"][("Base", "FWD")]
		current = frappe.db.get_value("BOM Item", {"parent": bom, "item_code": cat["wheel"]}, "qty")
		with as_user(ENGINEER):
			ecr = frappe.get_doc(
				{
					"doctype": "Engineering Change Request",
					"title": "Spare wheel",
					"change_reason": "Customer Request",
					"description": "Add a spare wheel",
					"affected_items": [
						{"item_code": cat["wheel"], "current_bom": bom, "proposed_change": "one more"}
					],
				}
			).insert()
			ecr.submit()
			from automotive_manufacturing.product_engineering.change_control import make_eco

			eco = make_eco(ecr.name)  # unsaved, prefilled with today's quantity
			self.assertTrue(eco.is_new())
			self.assertEqual((eco.company, eco.changes[0].qty), (AICHI, current))
			with self.assertRaises(frappe.ValidationError):  # an unchanged quantity is not a change
				eco.insert()
			eco.changes[0].qty = current + 1
			eco.insert()
		approve(eco, ENGINEER, [ENG_MANAGER, QM1])
		with as_user(ENG_MANAGER):
			eco.reload()
			eco.submit()
		self.assertEqual(frappe.db.get_value("Engineering Change Request", ecr.name, "status"), "Converted")

	def test_an_alternative_is_consumed_only_as_an_eco_approved_one_in_its_validity(self):
		"""ENT-11, ERP-02.1: substitution is an engineering decision. An ECO's "Approve Alternative"
		creates ERPNext's Item Alternative, linked to the ECO and valid from its effective date; a
		component consumed in production in place of the BOM's must be such an approved alternative,
		valid on the posting date. One entered by hand is not usable."""
		from automotive_manufacturing.product_engineering.bom_revisions import EngineeringControlError
		from automotive_manufacturing.product_engineering.change_control import validate_approved_alternatives

		cat = self.cat
		wheel = cat["wheel"]
		[bom] = frappe.db.sql(
			"""select b.name from `tabBOM` b join `tabBOM Item` bi on bi.parent = b.name
			where bi.item_code = %s and b.docstatus = 1 and b.is_active = 1 and ifnull(b.automotive_superseded_by, '') = ''
			order by b.name limit 1""",
			wheel,
			pluck=True,
		)

		def wheel_like(code):
			return catalog._item(
				code, "Raw Material", has_batch_no=1, create_new_batch=1, batch_number_series=f"{code}-.#####"
			)

		second_source = wheel_like(f"TWHA-{frappe.generate_hash(length=5).upper()}")

		def consumed(item, on):
			return frappe.get_doc(
				{
					"doctype": "Stock Entry",
					"purpose": "Manufacture",
					"company": AICHI,
					"posting_date": on,
					"items": [
						{
							"item_code": item,
							"original_item": wheel,
							"qty": 4,
							"s_warehouse": catalog.LINE_SIDE,
						}
					],
				}
			)

		with self.assertRaises(EngineeringControlError):  # nothing approved yet
			validate_approved_alternatives(consumed(second_source, nowdate()))
		effective = add_days(nowdate(), 2)
		with as_user(ENGINEER):
			eco = frappe.get_doc(
				{
					"doctype": "Engineering Change Order",
					"title": "Second-source wheel",
					"company": AICHI,
					"effective_date": effective,
					"changes": [
						{
							"change_type": "Approve Alternative",
							"target_bom": bom,
							"component": wheel,
							"new_component": second_source,
						}
					],
				}
			).insert()
		approve(eco, ENGINEER, [ENG_MANAGER, QM1])
		with as_user(ENG_MANAGER):
			eco.reload()
			eco.submit()
		approved = frappe.db.get_value(
			"Item Alternative",
			{"item_code": wheel, "alternative_item_code": second_source},
			["automotive_eco", "automotive_valid_from"],
			as_dict=True,
		)
		self.assertEqual(
			(approved.automotive_eco, getdate(approved.automotive_valid_from)), (eco.name, getdate(effective))
		)
		eco.reload()
		self.assertEqual(
			(eco.implementation_status, len(eco.revisions)), ("Implemented", 0)
		)  # no BOM revision
		with self.assertRaises(EngineeringControlError):  # before the change takes effect
			validate_approved_alternatives(consumed(second_source, nowdate()))
		validate_approved_alternatives(consumed(second_source, effective))
		by_hand = wheel_like(f"TWHB-{frappe.generate_hash(length=5).upper()}")
		frappe.get_doc(
			{"doctype": "Item Alternative", "item_code": wheel, "alternative_item_code": by_hand}
		).insert(ignore_permissions=True)
		with self.assertRaises(EngineeringControlError):
			validate_approved_alternatives(consumed(by_hand, effective))

	def test_an_operation_change_releases_a_new_routing_revision(self):
		"""ERP-02.1 routing versions: the operations travel with the BOM revision, so an ECO "Change
		Operation" releases a revision with the new time or workstation while the old revision, and
		the orders pinned to it, keep theirs."""
		cat = self.cat
		workstation = frappe.db.get_value(
			"Workstation", {"automotive_plant": catalog.PLANT}, "name", order_by="name"
		)
		if not frappe.db.exists("Operation", "TST Seat trimming"):
			frappe.get_doc(
				{"doctype": "Operation", "__newname": "TST Seat trimming", "workstation": workstation}
			).insert(ignore_permissions=True)
		old = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": cat["seat"],
				"company": AICHI,
				"quantity": 1,
				"is_default": 0,
				"is_active": 1,
				"with_operations": 1,
				"items": [{"item_code": cat["foam"], "qty": 1.5}],
				"operations": [
					{"operation": "TST Seat trimming", "workstation": workstation, "time_in_mins": 4}
				],
			}
		)
		old.insert(ignore_permissions=True)
		old.submit()
		change = {"change_type": "Change Operation", "target_bom": old.name, "operation": "TST Seat trimming"}
		with as_user(ENGINEER), self.assertRaises(frappe.ValidationError):  # nothing would change
			frappe.get_doc(
				{
					"doctype": "Engineering Change Order",
					"title": "Trimming unchanged",
					"company": AICHI,
					"effective_date": add_days(nowdate(), 1),
					"changes": [{**change, "time_in_mins": 4}],
				}
			).insert()
		with as_user(ENGINEER):
			eco = frappe.get_doc(
				{
					"doctype": "Engineering Change Order",
					"title": "Slower trimming for the new cover",
					"company": AICHI,
					"effective_date": add_days(nowdate(), 1),
					"changes": [{**change, "time_in_mins": 6}],
				}
			).insert()
		approve(eco, ENGINEER, [ENG_MANAGER, QM1])
		with as_user(ENG_MANAGER):
			eco.reload()
			eco.submit()
		eco.reload()
		new = frappe.get_doc("BOM", eco.revisions[0].new_bom)
		self.assertEqual(
			[(o.operation, flt(o.time_in_mins)) for o in new.operations], [("TST Seat trimming", 6)]
		)
		self.assertEqual(flt(frappe.db.get_value("BOM Operation", {"parent": old.name}, "time_in_mins")), 4)
		self.assertEqual(frappe.db.get_value("BOM", old.name, "automotive_superseded_by"), new.name)

	def test_impact_assessment_counts_open_requests_and_planning_proposals(self):
		"""ERP-02.3: besides orders, stock and work in progress, a change request's impact names what
		is requested and not yet ordered, and what the newest planning run proposes and has not
		released."""
		from automotive_manufacturing.product_engineering import change_control

		cat = self.cat
		wheel = cat["wheel"]
		before = change_control._open_request_qty([wheel])
		request = frappe.get_doc(
			{
				"doctype": "Material Request",
				"material_request_type": "Purchase",
				"company": AICHI,
				"schedule_date": add_days(nowdate(), 7),
				"items": [
					{
						"item_code": wheel,
						"qty": 5,
						"schedule_date": add_days(nowdate(), 7),
						"warehouse": catalog.PARTS_STORE,
					}
				],
			}
		)
		request.insert(ignore_permissions=True)
		request.submit()
		self.assertEqual(change_control._open_request_qty([wheel]), before + 5)
		params = frappe.get_doc(
			{
				"doctype": "Planning Parameter Set",
				"parameter_set": "AICHI-ECR-IMPACT",
				"company": AICHI,
				"plant": catalog.PLANT,
				"horizon_days": 21,
				"frozen_zone_days": 0,
				"lot_sizing": "Lot For Lot",
				"capacity_check": 0,
				"respect_holidays": 0,
				"include_forecast": 0,
			}
		)
		params.insert(ignore_permissions=True)
		approve(params, "Administrator", [DIRECTOR])
		params.reload()
		params.submit()
		# the plant's newest run: one open proposal for the wheel, one rejected, one already released
		frappe.get_doc(
			{
				"doctype": "Planning Run",
				"company": AICHI,
				"plant": catalog.PLANT,
				"parameter_set": params.name,
				"status": "Completed",
				"proposals": [
					{"proposal_type": "Purchase", "item_code": wheel, "qty": 7, "decision": "Proposed"},
					{"proposal_type": "Purchase", "item_code": wheel, "qty": 11, "decision": "Rejected"},
					{
						"proposal_type": "Purchase",
						"item_code": wheel,
						"qty": 13,
						"decision": "Approved",
						"document_type": "Material Request",
						"document_name": request.name,
					},
				],
			}
		).insert(ignore_permissions=True)
		with as_user(ENGINEER):
			ecr = frappe.get_doc(
				{
					"doctype": "Engineering Change Request",
					"title": "Wheel supplier change",
					"change_reason": "Cost Reduction",
					"description": "Second source for the wheel",
					"affected_items": [{"item_code": wheel, "proposed_change": "second source"}],
				}
			).insert()
			ecr.submit()
		ecr.reload()
		self.assertEqual((ecr.open_request_qty, ecr.proposal_qty), (before + 5, 7))
		self.assertIn("in planning proposals not yet released", ecr.impact_summary)


class TestBuildGenealogyAndRecall(ManufacturingTestCase):
	def test_vehicle_build_traceability_and_recall(self):
		cat = self.cat
		stock = self.stock_up(2)
		variant = cat["variants"][("Premium", "AWD")]
		wo = catalog.work_order(variant, 2)
		units = frappe.get_all(
			"Vehicle Unit", filters={"work_order": wo.name}, pluck="name", order_by="build_sequence"
		)
		self.assertEqual(len(units), 2)
		from automotive_manufacturing.engine.vin import is_valid

		self.assertTrue(all(is_valid(v) for v in units))
		for i, vin in enumerate(units):
			build_entry(
				wo.name,
				1,
				components=[
					{"item_code": cat["ecu"], "serial_no": stock["ecus"][i]},
					{"item_code": cat["seat"], "batch_no": stock["seat_batch"]},
				],
			)
			self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "production_status"), "Built")
			self.assertEqual(frappe.db.get_value("Serial No", vin, "warehouse"), catalog.FG)
			self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "quality_status"), "Pending Inspection")
			self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "invoice_status"), "Not Invoiced")
		tree = genealogy.components_of(units[0])
		self.assertIn(stock["ecus"][0], {r["component_serial_no"] for r in tree})
		self.assertIn(stock["foam_batch"], {r["component_batch_no"] for r in tree})  # two levels down
		affected = genealogy.units_containing(cat["foam"], batch_nos=[stock["foam_batch"]])
		self.assertEqual(sorted(a["serial_no"] for a in affected), sorted(units))
		recall = frappe.get_doc(
			{
				"doctype": "Recall Campaign",
				"title": "Foam lot",
				"component_item": cat["foam"],
				"batch_nos": stock["foam_batch"],
			}
		).insert()
		from automotive_manufacturing.traceability.recall import analyze

		analyze(recall)
		self.assertEqual(recall.affected_units, 2)
		first_hash = recall.result_sha256
		analyze(recall)
		self.assertEqual(recall.result_sha256, first_hash)  # reproducible
		# exactly-once stock effect: building more than ordered is refused
		with self.assertRaises(frappe.ValidationError):
			build_entry(wo.name, 1)

	def test_auto_numbered_output_is_linked_to_its_lots(self):
		"""A sub-assembly whose batch is numbered on posting (as the line reports it) still
		gets its genealogy, so vehicles trace through it to the supplier lots."""
		cat = self.cat
		foam_batch = catalog.make_batch(
			cat["foam"], f"TFOAM-{frappe.generate_hash(length=6)}", "Shiranui Foam Industries"
		)
		catalog.receive(cat["foam"], 2, batch_no=foam_batch, rate=40)
		wo = catalog.work_order(cat["seat"], 1, fg_warehouse=catalog.LINE_SIDE)
		se = build_entry(wo.name, 1)
		links = frappe.get_all(
			"Genealogy Link",
			filters={"stock_entry": se.name},
			fields=["parent_batch_no", "component_batch_no"],
		)
		self.assertTrue(links and all(link.parent_batch_no for link in links))
		self.assertIn(foam_batch, {link.component_batch_no for link in links})
		from automotive_manufacturing.traceability.genealogy import backfill

		backfill()  # entries that already have links are left alone
		self.assertEqual(frappe.db.count("Genealogy Link", {"stock_entry": se.name}), len(links))

	def test_unreleased_vehicle_cannot_ship(self):
		cat = self.cat
		self.stock_up(1)
		wo = catalog.work_order(cat["variants"][("Premium", "AWD")], 1)
		build_entry(wo.name, 1)
		vin = frappe.get_all("Vehicle Unit", filters={"work_order": wo.name}, pluck="name")[0]
		dn = frappe.new_doc("Delivery Note")
		dn.company = AICHI
		dn.customer = frappe.db.get_value(
			"Customer",
			{"is_internal_customer": 1, "represents_company": "Hinode Automotive Distribution North America"},
			"name",
		)
		dn.append(
			"items",
			{
				"item_code": wo.production_item,
				"qty": 1,
				"warehouse": catalog.FG,
				"use_serial_batch_fields": 1,
				"serial_no": vin,
				"rate": 1000,
			},
		)
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
			qi = frappe.get_doc(
				{
					"doctype": "Quality Inspection",
					"inspection_type": "In Process",
					"automotive_inspection_stage": "Final",
					"reference_type": "Stock Entry",
					"reference_name": se.name,
					"item_code": wo.production_item,
					"item_serial_no": vin,
					"sample_size": 1,
					"inspected_by": QM1,
					"status": status,
					"manual_inspection": 1,
					"company": AICHI,
				}
			)
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
		self.assertEqual(
			frappe.db.get_value("Vehicle Unit", vin, "quality_status"), "On Hold"
		)  # hold still active
		doc = frappe.get_doc("Quality Hold", hold)
		for check in doc.release_checks:
			check.done = 1
		doc.save(ignore_permissions=True)
		with as_user(QM2):
			holds.release(hold, "Seal replaced; water test passed")
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "quality_status"), "Released")
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "final_inspection"), again.name)
		self.assertEqual(
			frappe.db.get_value("Quality Inspection", rejected.name, "status"), "Rejected"
		)  # kept


class TestRoutedWorkOrders(ManufacturingTestCase):
	def test_the_mes_completes_an_operation_unless_its_work_order_is_held(self):
		"""ERP-04.1, ERP-04.4: the MES completes a Job Card in one call (time log and quantity, then
		submitted once the quantity is reached), and nothing is logged while the work order is held."""
		import uuid

		from frappe.utils import add_to_date, now_datetime

		from automotive_manufacturing.mes.production import complete_operation
		from automotive_manufacturing.quality_control.holds import QualityHoldError, raise_hold

		cat = self.cat
		workstation = frappe.db.get_value(
			"Workstation", {"automotive_plant": catalog.PLANT}, "name", order_by="name"
		)
		if not frappe.db.exists("Operation", "TST Seat stitching"):
			frappe.get_doc(
				{"doctype": "Operation", "__newname": "TST Seat stitching", "workstation": workstation}
			).insert(ignore_permissions=True)
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": cat["seat"],
				"company": AICHI,
				"quantity": 1,
				"is_default": 0,
				"is_active": 1,
				"with_operations": 1,
				"items": [{"item_code": cat["foam"], "qty": 1.5}],
				"operations": [
					{"operation": "TST Seat stitching", "workstation": workstation, "time_in_mins": 3}
				],
			}
		)
		bom.insert(ignore_permissions=True)
		bom.submit()
		wo = catalog.work_order(cat["seat"], 2, bom=bom.name, fg_warehouse=catalog.LINE_SIDE, pinned=True)
		[card] = frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="name")
		employee = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "Tst",
				"last_name": "Stitcher",
				"gender": "Female",
				"date_of_birth": "1991-01-01",
				"date_of_joining": "2021-01-01",
				"company": AICHI,
				"create_user_permission": 0,
			}
		).insert(ignore_permissions=True)
		# two days back: clear of the time logs other tests record on the same station now
		start = add_to_date(now_datetime(), days=-2)
		end = add_to_date(start, minutes=6)
		hold = raise_hold("Work Order", "Suspect foam lot on this order", AICHI, work_order=wo.name)
		with self.assertRaises(QualityHoldError):
			complete_operation(card, 2, employee.name, start, end, idempotency_key=str(uuid.uuid4()))
		self.assertFalse(frappe.get_all("Job Card Time Log", filters={"parent": card}))
		frappe.db.set_value("Quality Hold", hold, "status", "Released")
		result = complete_operation(card, 2, employee.name, start, end, idempotency_key=str(uuid.uuid4()))
		self.assertEqual((result["job_card"], result["completed_qty"]), (card, 2))
		self.assertEqual(frappe.db.get_value("Job Card", card, "docstatus"), 1)

	def test_units_post_only_after_their_operations_are_confirmed(self):
		"""ERP-04.1: a routed work order gets a job card per operation; its units post only once
		the station confirms them, and the time logs record who did the work."""
		from frappe.utils import add_to_date, now_datetime

		from automotive_manufacturing.mes.production import record_operation

		cat = self.cat
		workstation = frappe.db.get_value(
			"Workstation", {"automotive_plant": catalog.PLANT}, "name", order_by="name"
		)
		if not frappe.db.exists("Operation", "TST Seat sewing"):
			frappe.get_doc(
				{"doctype": "Operation", "__newname": "TST Seat sewing", "workstation": workstation}
			).insert(ignore_permissions=True)
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": cat["seat"],
				"company": AICHI,
				"quantity": 1,
				"is_default": 0,
				"is_active": 1,
				"with_operations": 1,
				"items": [{"item_code": cat["foam"], "qty": 1.5}],
				"operations": [
					{"operation": "TST Seat sewing", "workstation": workstation, "time_in_mins": 4}
				],
			}
		)
		bom.insert(ignore_permissions=True)
		bom.submit()
		foam_batch = catalog.make_batch(
			cat["foam"], f"TFOAM-{frappe.generate_hash(length=6)}", "Shiranui Foam Industries"
		)
		catalog.receive(cat["foam"], 3, batch_no=foam_batch, rate=40)
		wo = catalog.work_order(cat["seat"], 2, bom=bom.name, fg_warehouse=catalog.LINE_SIDE, pinned=True)
		cards = frappe.get_all("Job Card", filters={"work_order": wo.name}, fields=["name", "for_quantity"])
		self.assertEqual([c.for_quantity for c in cards], [2])
		with self.assertRaises(frappe.ValidationError):
			build_entry(wo.name, 1)  # the station has not confirmed anything
		employee = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "Tst",
				"last_name": "Sewer",
				"gender": "Female",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"company": AICHI,
				"create_user_permission": 0,
			}
		).insert(ignore_permissions=True)
		end = now_datetime()
		self.assertEqual(
			record_operation(cards[0].name, 2, employee.name, add_to_date(end, minutes=-8), end), "Completed"
		)
		build_entry(wo.name, 1)
		build_entry(wo.name, 1)
		self.assertEqual(frappe.db.get_value("Work Order", wo.name, "produced_qty"), 2)
		self.assertEqual(
			frappe.get_all(
				"Job Card Time Log",
				filters={"parent": cards[0].name, "parentfield": "time_logs"},
				fields=["employee", "completed_qty"],
			),
			[{"employee": employee.name, "completed_qty": 2}],
		)

	def test_the_station_rate_costs_the_operation_time_into_the_units(self):
		"""ERP-09.3: a routed order's operation is costed at its station's rate per hour, planned
		from the BOM's time and actual from the time the station logged; each unit built carries
		its share as an additional cost of the Manufacture entry, into its valuation and the
		operating cost account."""
		from frappe.utils import add_to_date, now_datetime

		from automotive_core.setup.tenant import OPERATING_COMPONENT
		from automotive_manufacturing.mes.production import record_operation

		cat = self.cat
		station = "TST Costed Sewing Station"
		if not frappe.db.exists("Workstation", station):
			# the plant's calendar and working hours, as the tenant's stations have them
			template = frappe.get_doc(
				"Workstation",
				frappe.db.get_value(
					"Workstation", {"automotive_plant": catalog.PLANT}, "name", order_by="name"
				),
			)
			doc = frappe.copy_doc(template)
			doc.workstation_name = station
			doc.set(
				"workstation_costs", [{"operating_component": OPERATING_COMPONENT, "operating_cost": 6_000}]
			)
			doc.insert(ignore_permissions=True)
		self.assertEqual(flt(frappe.db.get_value("Workstation", station, "hour_rate")), 6_000)
		if not frappe.db.exists("Operation", "TST Costed sewing"):
			frappe.get_doc(
				{"doctype": "Operation", "__newname": "TST Costed sewing", "workstation": station}
			).insert(ignore_permissions=True)
		bom = frappe.get_doc(
			{
				"doctype": "BOM",
				"item": cat["seat"],
				"company": AICHI,
				"quantity": 1,
				"is_default": 0,
				"is_active": 1,
				"with_operations": 1,
				"rm_cost_as_per": "Valuation Rate",
				"items": [{"item_code": cat["foam"], "qty": 1.5}],
				"operations": [{"operation": "TST Costed sewing", "workstation": station, "time_in_mins": 4}],
			}
		)
		bom.insert(ignore_permissions=True)
		bom.submit()
		self.assertEqual(flt(bom.operating_cost), 400)  # 4 minutes at 6,000 an hour
		foam_batch = catalog.make_batch(
			cat["foam"], f"TFOAM-{frappe.generate_hash(length=6)}", "Shiranui Foam Industries"
		)
		catalog.receive(cat["foam"], 3, batch_no=foam_batch, rate=40)
		wo = catalog.work_order(cat["seat"], 2, bom=bom.name, fg_warehouse=catalog.LINE_SIDE, pinned=True)
		self.assertEqual(flt(wo.planned_operating_cost), 800)
		[card] = frappe.get_all("Job Card", filters={"work_order": wo.name}, pluck="name")
		employee = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "Tst",
				"last_name": "Costed",
				"gender": "Female",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-01-01",
				"company": AICHI,
				"create_user_permission": 0,
			}
		).insert(ignore_permissions=True)
		end = now_datetime()
		# the station logged 9 minutes for the two seats, not the 8 the BOM plans
		record_operation(card, 2, employee.name, add_to_date(end, minutes=-9), end)
		[operation] = frappe.get_doc("Work Order", wo.name).operations
		self.assertEqual(flt(operation.actual_operation_time), 9)
		self.assertEqual(flt(operation.actual_operating_cost), 900)
		for _ in range(2):
			entry = build_entry(wo.name, 1)
			[seat] = [row for row in entry.items if row.is_finished_item]
			self.assertEqual(flt(entry.total_additional_costs), 450)  # 900 over the two seats
			self.assertAlmostEqual(flt(seat.valuation_rate), 1.5 * 40 + 450, places=2)
			# the cost's accounts are credited what the stock was debited for it
			accounts = tuple({row.expense_account for row in entry.additional_costs})
			credited = flt(
				frappe.db.sql(
					"""select sum(credit - debit) from `tabGL Entry` where voucher_type = 'Stock Entry'
					and voucher_no = %s and account in %s and is_cancelled = 0""",
					(entry.name, accounts),
				)[0][0]
			)
			self.assertAlmostEqual(credited, 450, places=2)


AICHI_MES = "mes.aichi.mfg@hinode.example"
KYUSHU_MES = "mes.kyushu.mfg@hinode.example"


class TestProductionEvents(ManufacturingTestCase):
	"""ERP-04.1 / ERP-11: a plant MES reports builds as production_event messages; the hub runs
	the registered handler as the partner's service user, which posts the Manufacture entry."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for user, code in ((AICHI_MES, "TEST-MES-AICHI"), (KYUSHU_MES, "TEST-MES-KYUSHU")):
			make_user(user, ["Production Supervisor", "Manufacturing User"])
			frappe.get_doc(
				{
					"doctype": "Integration Partner",
					"partner_code": code,
					"partner_name": code,
					"partner_type": "Internal",
					"adapter": "Local File Drop",
					"message_format": "JSON",
					"allowed_message_types": "production_event",
					"service_user": user,
				}
			).insert(ignore_permissions=True)
		# the Kyushu line system's account sees its own plant only
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": KYUSHU_MES,
				"allow": "Plant",
				"for_value": "KYUSHU-PT",
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)

	def setUp(self):
		frappe.flags.automotive_pending_inbound = []

	def report(self, partner: str, message_id: str, data: dict):
		accepted = inbound.accept(
			partner, json.dumps({"message_id": message_id, "message_type": "production_event", "data": data})
		)
		inbound.run_pending_inline()
		return frappe.db.get_value(
			"Integration Inbound Message",
			accepted["inbound_id"],
			["status", "result_doctype", "result_name", "last_error"],
			as_dict=True,
		)

	def test_a_reported_unit_is_built_and_bad_reports_are_rejected(self):
		self.assertEqual(
			hook_dict("automotive_inbound_handlers")["production_event"],
			"automotive_manufacturing.mes.handlers.production_event",
		)
		self.stock_up(1)
		wo = catalog.work_order(self.cat["variants"][("Premium", "AWD")], 1)
		[vin] = frappe.get_all("Vehicle Unit", filters={"work_order": wo.name}, pluck="name")
		built = self.report(
			"TEST-MES-AICHI", "MES-AICHI-0001", {"event": "unit_built", "work_order": wo.name}
		)
		self.assertEqual((built.status, built.result_doctype), ("Processed", "Stock Entry"), built.last_error)
		entry = frappe.get_doc("Stock Entry", built.result_name)
		self.assertEqual((entry.docstatus, entry.purpose, entry.work_order), (1, "Manufacture", wo.name))
		self.assertEqual(entry.owner, AICHI_MES)  # posted as the partner's service user
		self.assertEqual(flt(frappe.db.get_value("Work Order", wo.name, "produced_qty")), 1)
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "production_status"), "Built")
		# an order the plant does not know, and a report outside the message contract: never retried
		unknown = self.report(
			"TEST-MES-AICHI", "MES-AICHI-0002", {"event": "unit_built", "work_order": "WO-NO-SUCH"}
		)
		self.assertEqual(unknown.status, "Rejected")
		self.assertIn("unknown work order WO-NO-SUCH", unknown.last_error)
		invalid = self.report(
			"TEST-MES-AICHI", "MES-AICHI-0003", {"event": "batch_built", "work_order": wo.name, "qty": 0}
		)
		self.assertEqual(invalid.status, "Rejected")
		self.assertIn("invalid production_event", invalid.last_error)
		self.assertEqual(frappe.db.count("Stock Entry", {"work_order": wo.name, "docstatus": 1}), 1)

	def test_a_plant_system_cannot_report_another_plants_order(self):
		wo = catalog.work_order(self.cat["variants"][("Premium", "AWD")], 1)  # an Aichi order
		result = self.report(
			"TEST-MES-KYUSHU", "MES-KYU-0001", {"event": "unit_built", "work_order": wo.name}
		)
		self.assertEqual(result.status, "Rejected")
		self.assertIn(f"may not report on work order {wo.name}", result.last_error)
		self.assertFalse(frappe.db.exists("Stock Entry", {"work_order": wo.name}))
		self.assertEqual(
			frappe.db.get_value("Vehicle Unit", {"work_order": wo.name}, "production_status"), "Planned"
		)


class TestShiftHandover(ManufacturingTestCase):
	def test_handover_reports_the_shift_and_only_the_incoming_supervisor_acknowledges(self):
		"""ERP-04.5: results are computed at hand-over; the next shift's supervisor confirms."""
		from automotive_manufacturing.shop_floor.handover import acknowledge, shift_window

		incoming = "supervisor2.mfg@hinode.example"
		make_user(incoming, ["Production Supervisor", "Manufacturing User"])
		cat = self.cat
		self.stock_up(1)
		wo = catalog.work_order(cat["variants"][("Premium", "AWD")], 1)
		build_entry(wo.name, 1)
		shift = (
			frappe.get_cached_doc(
				"Shift Pattern", frappe.get_cached_value("Production Line", catalog.LINE, "shift_pattern")
			)
			.shifts[0]
			.shift_code
		)
		start, _end = shift_window(catalog.LINE, nowdate(), shift)
		frappe.db.set_value(
			"Vehicle Unit", {"work_order": wo.name}, "built_at", start + datetime.timedelta(hours=1)
		)
		values = {
			"doctype": "Shift Handover",
			"plant": catalog.PLANT,
			"production_line": catalog.LINE,
			"shift_date": nowdate(),
			"shift_code": shift,
			"handover_notes": "Line running to plan.",
		}
		with as_user(SUPERVISOR), self.assertRaises(frappe.ValidationError):
			frappe.get_doc({**values, "incoming_supervisor": SUPERVISOR}).insert()
		with as_user(SUPERVISOR):
			doc = frappe.get_doc({**values, "incoming_supervisor": incoming}).insert()
			doc.submit()
		self.assertEqual((doc.status, doc.outgoing_supervisor), ("Handed Over", SUPERVISOR))
		self.assertGreaterEqual(doc.built_units, 1)  # the unit built in the shift's window
		with as_user(SUPERVISOR), self.assertRaises(frappe.PermissionError):
			acknowledge(doc.name)  # the outgoing supervisor cannot confirm their own handover
		with as_user(incoming):
			self.assertEqual(acknowledge(doc.name), "Acknowledged")
		self.assertTrue(frappe.db.get_value("Shift Handover", doc.name, "acknowledged_at"))


class TestJidoka(ManufacturingTestCase):
	def test_out_of_tolerance_reading_rejects_the_lot(self):
		"""ERP-07.1: with an inspection plan the readings decide; a re-inspection needs new readings."""
		from automotive_manufacturing.quality_control.inspections import make_reinspection

		cat = self.cat
		for parameter in ("TST Rim runout (mm)", "TST Visual"):
			if not frappe.db.exists("Quality Inspection Parameter", parameter):
				frappe.get_doc({"doctype": "Quality Inspection Parameter", "parameter": parameter}).insert(
					ignore_permissions=True
				)
		if not frappe.db.exists("Quality Inspection Template", "TST Incoming Wheels"):
			frappe.get_doc(
				{
					"doctype": "Quality Inspection Template",
					"quality_inspection_template_name": "TST Incoming Wheels",
					"item_quality_inspection_parameter": [
						{
							"specification": "TST Rim runout (mm)",
							"numeric": 1,
							"min_value": 0,
							"max_value": 0.5,
						},
						{"specification": "TST Visual", "numeric": 0, "value": "OK"},
					],
				}
			).insert(ignore_permissions=True)
		frappe.db.set_value("Item", cat["wheel"], "quality_inspection_template", "TST Incoming Wheels")
		batch = catalog.make_batch(
			cat["wheel"], f"TWHL-{frappe.generate_hash(length=6)}", "Shiranui Foam Industries"
		)
		receipt = catalog.receive(cat["wheel"], 4, warehouse=catalog.PARTS_STORE, batch_no=batch, rate=120)

		def readings(*runout: str, visual: str = "OK") -> list[dict]:
			return [
				{
					"specification": "TST Rim runout (mm)",
					"numeric": 1,
					"min_value": 0,
					"max_value": 0.5,
					**{f"reading_{i}": value for i, value in enumerate(runout, 1)},
				},
				{"specification": "TST Visual", "numeric": 0, "value": "OK", "reading_value": visual},
			]

		qi = frappe.get_doc(
			{
				"doctype": "Quality Inspection",
				"inspection_type": "Incoming",
				"automotive_inspection_stage": "Incoming",
				"reference_type": "Stock Entry",
				"reference_name": receipt,
				"item_code": cat["wheel"],
				"batch_no": batch,
				"sample_size": 2,
				"inspected_by": "Administrator",
				"quality_inspection_template": "TST Incoming Wheels",
				"manual_inspection": 0,
				"company": AICHI,
				"readings": readings("0.21", "0.74"),
			}
		)
		qi.insert(ignore_permissions=True)
		self.assertEqual(
			(qi.status, qi.readings[0].status, qi.readings[1].status), ("Rejected", "Rejected", "Accepted")
		)
		qi.submit()
		qi.reload()
		self.assertEqual(frappe.db.get_value("Quality Hold", qi.automotive_quality_hold, "status"), "Active")
		with as_user(QM2):
			unmeasured = frappe.get_doc("Quality Inspection", make_reinspection(qi.name))
			measured = make_reinspection(qi.name, readings=readings("0.18", "0.22"))
		self.assertEqual(unmeasured.status, "Rejected")  # no readings recorded: nothing to accept
		self.assertEqual(frappe.db.get_value("Quality Inspection", measured, "status"), "Accepted")

	def test_rejected_batch_is_held_until_dispositioned(self):
		cat = self.cat
		batch = catalog.make_batch(
			cat["wheel"], f"TWHL-{frappe.generate_hash(length=6)}", "Shiranui Foam Industries"
		)
		receipt = catalog.receive(cat["wheel"], 8, warehouse=catalog.PARTS_STORE, batch_no=batch, rate=120)
		qi = frappe.get_doc(
			{
				"doctype": "Quality Inspection",
				"inspection_type": "Incoming",
				"automotive_inspection_stage": "Incoming",
				"reference_type": "Stock Entry",
				"reference_name": receipt,
				"item_code": cat["wheel"],
				"batch_no": batch,
				"sample_size": 2,
				"inspected_by": "Administrator",
				"status": "Rejected",
				"manual_inspection": 1,
				"company": AICHI,
			}
		)
		qi.insert(ignore_permissions=True)
		qi.submit()
		qi.reload()
		hold = qi.automotive_quality_hold
		self.assertEqual(frappe.db.get_value("Quality Hold", hold, "status"), "Active")
		self.assertTrue(qi.automotive_nonconformance)
		move = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Material Transfer",
				"stock_entry_type": "Material Transfer",
				"company": AICHI,
				"items": [
					{
						"item_code": cat["wheel"],
						"qty": 8,
						"s_warehouse": catalog.PARTS_STORE,
						"t_warehouse": catalog.LINE_SIDE,
						"use_serial_batch_fields": 1,
						"batch_no": batch,
					}
				],
			}
		)
		with self.assertRaises(QualityHoldError):
			move.insert(ignore_permissions=True)
		# segregating into quarantine is always allowed
		quarantine = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Material Transfer",
				"stock_entry_type": "Material Transfer",
				"company": AICHI,
				"items": [
					{
						"item_code": cat["wheel"],
						"qty": 8,
						"s_warehouse": catalog.PARTS_STORE,
						"t_warehouse": catalog.QUARANTINE,
						"use_serial_batch_fields": 1,
						"batch_no": batch,
					}
				],
			}
		)
		quarantine.insert(ignore_permissions=True)
		quarantine.submit()
		with as_user(QM1):
			disposition = frappe.get_doc(
				{
					"doctype": "Quality Disposition",
					"quality_hold": hold,
					"nonconformance": qi.automotive_nonconformance,
					"company": AICHI,
					"decision": "Scrap",
					"item_code": cat["wheel"],
					"batch_no": batch,
					"qty": 8,
					"from_warehouse": catalog.QUARANTINE,
					"justification": "Cracked rims on 2 of 2 samples",
				}
			).insert()
		approve(disposition, QM1, [QM2])
		with as_user(QM1):
			disposition.reload()
			disposition.submit()
		disposition.reload()
		self.assertEqual(
			frappe.db.get_value("Stock Entry", disposition.stock_entry, "automotive_source_name"),
			disposition.name,
		)
		self.assertEqual(frappe.db.get_value("Quality Hold", hold, "status"), "Dispositioned")
		self.assertEqual(
			flt(
				frappe.db.get_value(
					"Bin", {"item_code": cat["wheel"], "warehouse": catalog.QUARANTINE}, "actual_qty"
				)
			),
			0,
		)
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
			andon = frappe.get_doc(
				{
					"doctype": "Andon Event",
					"production_line": catalog.LINE,
					"workstation": "AICHI-FA1-S30",
					"reason_code": "Q-DEFECT",
					"stop_type": "Line Stop",
					"work_order": wo.name,
					"description": "Loose harness",
				}
			).insert()
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
		loop = frappe.get_doc(
			{
				"doctype": "Kanban Loop",
				"loop_code": "KL-TEST-WHEEL",
				"item_code": cat["wheel"],
				"plant": catalog.PLANT,
				"production_line": catalog.LINE,
				"supply_type": "Internal Transfer",
				"line_side_warehouse": catalog.LINE_SIDE,
				"source_warehouse": catalog.PARTS_STORE,
				"container_qty": 4,
				"number_of_cards": 3,
				"lead_time_hours": 2,
				"daily_demand": 24,
				"delivery_windows": [
					{"weekday": d, "window_start": "08:00:00", "window_end": "20:00:00"}
					for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
				],
			}
		).insert(ignore_permissions=True)
		self.assertEqual(frappe.db.count("Kanban Card", {"kanban_loop": loop.name, "status": "Full"}), 3)
		stock = self.stock_up(1)  # puts 4 wheels at line side
		catalog.receive(cat["wheel"], 8, rate=120)  # 12 wheels = 3 containers at line side
		wo = catalog.work_order(cat["variants"][("Premium", "AWD")], 1)
		build_entry(wo.name, 1, components=[{"item_code": cat["ecu"], "serial_no": stock["ecus"][0]}])
		# 4 wheels consumed -> 8 left -> only 2 containers -> one card signals
		signals = frappe.get_all(
			"Kanban Signal",
			filters={"kanban_loop": loop.name},
			fields=["name", "status", "stock_entry", "signal_source"],
		)
		self.assertEqual(len(signals), 1)
		self.assertEqual(signals[0].signal_source, "Consumption Trigger")
		transfer = frappe.get_doc("Stock Entry", signals[0].stock_entry)
		transfer.submit()
		self.assertEqual(frappe.db.get_value("Kanban Signal", signals[0].name, "status"), "Received")
		self.assertEqual(frappe.db.count("Kanban Card", {"kanban_loop": loop.name, "status": "Full"}), 3)


class TestKanbanEscalation(ManufacturingTestCase):
	def test_an_overdue_signal_escalates_once_and_the_plant_supervisor_hears_of_it(self):
		"""J-02 stockout risk: a replenishment still open past its due time is escalated to level 1
		and the plant's supervisors are told; later runs leave it there, and a signal still within
		its window is not touched."""
		from automotive_core.governance import notifications
		from automotive_manufacturing.shop_floor import kanban

		notifications.ensure_templates()
		# a supervisor of its own: the plant assignment scopes the user (User Permissions, cached)
		lead = make_user("line.lead.mfg@hinode.example", ["Production Supervisor", "Manufacturing User"])
		employee = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "Ren",
				"last_name": "Hoshino",
				"gender": "Female",
				"date_of_birth": "1988-02-03",
				"date_of_joining": "2016-04-01",
				"company": AICHI,
				"user_id": lead,
			}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Plant Assignment",
				"employee": employee.name,
				"plant": catalog.PLANT,
				"assignment_role": "Supervisor",
				"from_date": add_days(nowdate(), -30),
			}
		).insert(ignore_permissions=True)
		catalog.receive(self.cat["wheel"], 8, warehouse=catalog.PARTS_STORE, rate=120)
		loop = frappe.get_doc(
			{
				"doctype": "Kanban Loop",
				"loop_code": "KL-TEST-ESCALATE",
				"item_code": self.cat["wheel"],
				"plant": catalog.PLANT,
				"production_line": catalog.LINE,
				"supply_type": "Internal Transfer",
				"line_side_warehouse": catalog.LINE_SIDE,
				"source_warehouse": catalog.PARTS_STORE,
				"container_qty": 4,
				"number_of_cards": 2,
				"lead_time_hours": 2,
			}
		).insert(ignore_permissions=True)
		late = kanban.scan_card(f"{loop.name}-01")
		on_time = kanban.scan_card(f"{loop.name}-02")
		self.assertEqual((late["status"], on_time["status"]), ("Ordered", "Ordered"))  # transfers requested
		frappe.db.set_value("Kanban Signal", late["signal"], "due_by", add_to_date(now_datetime(), hours=-3))
		kanban.escalate_overdue()
		kanban.escalate_overdue()  # the next run, five minutes later
		signal = frappe.db.get_value(
			"Kanban Signal", late["signal"], ["status", "escalation_level", "escalation_note"], as_dict=True
		)
		self.assertEqual((signal.status, cint(signal.escalation_level)), ("Escalated", 1))
		self.assertIn("Overdue since", signal.escalation_note)
		pending = frappe.db.get_value(
			"Kanban Signal", on_time["signal"], ["status", "escalation_level"], as_dict=True
		)
		self.assertEqual((pending.status, cint(pending.escalation_level)), ("Ordered", 0))
		self.assertEqual(frappe.db.get_value("Kanban Card", f"{loop.name}-01", "status"), "Signaled")
		# told once: a desk alert (the event sends no e-mail by default), not repeated by the second run
		told = frappe.get_all(
			"Notification Dispatch",
			filters={"event": "kanban.overdue", "recipient": lead},
			fields=["reference_name", "channel", "status"],
		)
		self.assertEqual(
			sorted((r.reference_name, r.channel, r.status) for r in told),
			[(late["signal"], "Email", "Suppressed"), (late["signal"], "In-App", "Delivered")],
		)
		self.assertTrue(
			frappe.db.exists("Notification Log", {"for_user": lead, "document_name": late["signal"]})
		)


class TestPlanning(ManufacturingTestCase):
	def test_a_run_left_running_by_a_dead_worker_is_failed_for_a_retry(self):
		"""PRF-8: the hourly recovery turns a run that has stayed Running for more than two hours
		(its worker died) into Failed, to be started again; a run still working is left alone."""
		from automotive_manufacturing.production_planning import planning_run

		params = frappe.get_doc(
			{
				"doctype": "Planning Parameter Set",
				"parameter_set": "AICHI-STALL",
				"company": AICHI,
				"plant": catalog.PLANT,
				"horizon_days": 7,
				"frozen_zone_days": 0,
				"lot_sizing": "Lot For Lot",
				"capacity_check": 0,
				"respect_holidays": 0,
				"include_forecast": 0,
			}
		)
		params.insert(ignore_permissions=True)
		approve(params, "Administrator", [DIRECTOR])
		params.reload()
		params.submit()

		def running():
			return (
				frappe.get_doc(
					{
						"doctype": "Planning Run",
						"company": AICHI,
						"plant": catalog.PLANT,
						"parameter_set": params.name,
						"status": "Running",
					}
				)
				.insert(ignore_permissions=True)
				.name
			)

		stalled, working = running(), running()
		frappe.db.set_value(
			"Planning Run", stalled, "modified", add_to_date(now_datetime(), hours=-3), update_modified=False
		)
		planning_run.recover_stalled()
		self.assertEqual(
			frappe.db.get_value("Planning Run", stalled, ["status", "error"]),
			("Failed", "worker stopped; retry the run"),
		)
		self.assertEqual(frappe.db.get_value("Planning Run", working, "status"), "Running")

	def test_the_weekly_production_plan_sets_work_orders_against_orders_and_capacity(self):
		"""ERP-03.3: the weekly MPS view: per ISO week and line, the firm orders due, the units the
		work orders plan and the line's capacity on its working days, with the week flagged when
		planned beyond capacity or short of the orders."""
		from frappe.desk.query_report import run

		cat = self.cat
		variant = cat["variants"][("Base", "FWD")]
		start = getdate(add_days(nowdate(), 21))
		monday = start - datetime.timedelta(days=start.weekday())
		offset = (monday - getdate(nowdate())).days + 1  # the Tuesday of that week
		line = frappe.get_doc("Production Line", catalog.LINE)
		days = len(snapshot.working_days(catalog.PLANT, monday, monday + datetime.timedelta(days=6)))
		capacity = int(line.max_units_per_day or 0) * days

		def week():
			rows = run(
				"Weekly Production Plan",
				filters={"company": AICHI, "plant": catalog.PLANT, "from_date": str(monday), "weeks": 1},
			)["result"]
			return next(r for r in rows if r["production_line"] == catalog.LINE)

		before = week()
		self.assertEqual((getdate(before["week"]), before["capacity"]), (monday, capacity))
		catalog.work_order(variant, 2, start_offset_days=offset)
		after = week()
		self.assertEqual(after["planned_qty"], before["planned_qty"] + 2)
		customer = "Weekly Plan Test Dealer"
		if not frappe.db.exists("Customer", customer):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": customer,
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
					"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
				}
			).insert(ignore_permissions=True)
		due = add_days(nowdate(), offset)
		order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": AICHI,
				"customer": customer,
				"transaction_date": nowdate(),
				"delivery_date": due,
				"items": [
					{
						"item_code": variant,
						"qty": after["planned_qty"] + 3,
						"rate": 2_000_000,
						"warehouse": catalog.FG,
						"delivery_date": due,
					}
				],
			}
		)
		order.insert(ignore_permissions=True)
		order.submit()
		short = week()
		self.assertGreaterEqual(short["firm_orders"], after["planned_qty"] + 3)
		self.assertIn("Short of firm orders", short["status"])

	def test_the_master_production_schedule_is_the_forecast_firm_orders_consume(self):
		"""ERP-03.1: with forecasts included, the native Master Production Schedule adds demand; firm
		orders consume it week by week, so a week asks for the larger of forecast and orders, not
		their sum. Without consumption the forecast is added in full; without forecasts it is not."""
		cat = self.cat
		variant = cat["variants"][("Base", "FWD")]
		start = getdate(add_days(nowdate(), 7))
		monday = start - datetime.timedelta(days=start.weekday())
		week1, week2 = monday + datetime.timedelta(days=1), monday + datetime.timedelta(days=8)
		customer = "Forecast Test Dealer"
		if not frappe.db.exists("Customer", customer):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": customer,
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
					"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
				}
			).insert(ignore_permissions=True)
		order = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": AICHI,
				"customer": customer,
				"transaction_date": nowdate(),
				"delivery_date": week1,
				"items": [
					{
						"item_code": variant,
						"qty": 2,
						"rate": 2_000_000,
						"warehouse": catalog.FG,
						"delivery_date": week1,
					}
				],
			}
		)
		order.insert(ignore_permissions=True)
		order.submit()
		mps = frappe.get_doc(
			{
				"doctype": "Master Production Schedule",
				"company": AICHI,
				"posting_date": nowdate(),
				"from_date": nowdate(),
				"items": [
					{
						"item_code": variant,
						"warehouse": catalog.FG,
						"delivery_date": week1,
						"planned_qty": 5,
						"uom": "Nos",
					},
					{
						"item_code": variant,
						"warehouse": catalog.FG,
						"delivery_date": week2,
						"planned_qty": 3,
						"uom": "Nos",
					},
				],
			}
		)
		mps.insert(ignore_permissions=True)
		run = frappe._dict(
			as_of=nowdate(), company=AICHI, plant=catalog.PLANT, master_production_schedule=mps.name
		)

		def demand(**params):
			rows = [
				d
				for d in snapshot.collect(run, frappe._dict(horizon_days=28, **params))["demand"]
				if d["item_code"] == variant
			]
			firm = sum(
				d["qty"]
				for d in rows
				if d["source"].startswith("SO:")
				and getdate(d["date"]).isocalendar()[1] == week1.isocalendar()[1]
			)
			return firm, sorted((d["date"], d["qty"]) for d in rows if d["source"] == f"MPS:{mps.name}")

		firm, forecast = demand(include_forecast=1, consume_forecast=1)
		self.assertGreaterEqual(firm, 2)
		self.assertEqual(
			forecast, [(str(week1), max(5 - firm, 0)), (str(week2), 3)] if firm < 5 else [(str(week2), 3)]
		)
		self.assertEqual(
			demand(include_forecast=1, consume_forecast=0)[1], [(str(week1), 5), (str(week2), 3)]
		)
		self.assertEqual(demand(include_forecast=0, consume_forecast=1)[1], [])

	def test_planning_run_is_reproducible_and_releases_native_documents(self):
		from automotive_manufacturing.production_planning import planning_run

		cat = self.cat
		params = frappe.get_doc(
			{
				"doctype": "Planning Parameter Set",
				"parameter_set": "AICHI-TEST",
				"company": AICHI,
				"plant": catalog.PLANT,
				"horizon_days": 21,
				"frozen_zone_days": 0,
				"lot_sizing": "Lot For Lot",
				"capacity_check": 1,
				"respect_holidays": 0,
				"include_forecast": 0,
			}
		)
		params.insert(ignore_permissions=True)
		approve(params, "Administrator", [DIRECTOR])
		params.reload()
		params.submit()
		customer = "Planning Test Dealer"
		if not frappe.db.exists("Customer", customer):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": customer,
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
					"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
				}
			).insert(ignore_permissions=True)
		so = frappe.get_doc(
			{
				"doctype": "Sales Order",
				"company": AICHI,
				"customer": customer,
				"transaction_date": nowdate(),
				"delivery_date": add_days(nowdate(), 14),
				"items": [
					{
						"item_code": cat["variants"][("Premium", "AWD")],
						"qty": 3,
						"rate": 2_500_000,
						"warehouse": catalog.FG,
						"delivery_date": add_days(nowdate(), 14),
					}
				],
			}
		)
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
		# the shortage-risk KPI reads the plant's latest run (not its replay) and reconciles
		from automotive_manufacturing.analytics import kpis

		risk = kpis.shortage_risk(AICHI, nowdate(), nowdate(), catalog.PLANT)
		self.assertEqual(risk.inputs["runs"], [run_name])
		self.assertEqual(risk.numerator, frappe.db.count("Planning Run Shortage", {"parent": run_name}))
		ok, detail = kpis.reconcile_shortage_risk(AICHI, nowdate(), nowdate(), catalog.PLANT, risk)
		self.assertTrue(ok, detail)


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
		changes = frappe.get_all(
			"Plant Schedule Change", filters={"plant_schedule": name}, pluck="change_type"
		)
		self.assertEqual(sorted(changes), ["Release", "Resequence"])


class TestPlantScopedActions(ManufacturingTestCase):
	"""SEC-3: the shop-floor and planning actions write past the DocType permissions, so each one
	checks the caller's User Permissions on the document it acts on: a supervisor of another plant
	cannot acknowledge an Aichi andon or rebuild an Aichi line's schedule, and a user whose role
	may not plan cannot build a schedule at all."""

	KYUSHU_SUPERVISOR = "supervisor.kyushu.mfg@hinode.example"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(cls.KYUSHU_SUPERVISOR, ["Production Supervisor", "Manufacturing User"])
		if not frappe.db.exists("User Permission", {"user": cls.KYUSHU_SUPERVISOR, "allow": "Plant"}):
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": cls.KYUSHU_SUPERVISOR,
					"allow": "Plant",
					"for_value": "KYUSHU-PT",
					"apply_to_all_doctypes": 1,
				}
			).insert(ignore_permissions=True)

	def test_another_plant_s_documents_are_out_of_reach(self):
		from automotive_manufacturing.production_planning import schedule
		from automotive_manufacturing.shop_floor import andon as andon_service

		operator = make_user("operator.scope.mfg@hinode.example", ["Line Operator"])
		with as_user(operator):
			andon = frappe.get_doc(
				{
					"doctype": "Andon Event",
					"production_line": catalog.LINE,
					"workstation": "AICHI-FA1-S30",
					"reason_code": "P-HELP",
					"stop_type": "Call",
					"description": "Help needed with the harness routing",
				}
			).insert()
		with as_user(self.KYUSHU_SUPERVISOR), self.assertRaises(frappe.PermissionError):
			andon_service.acknowledge(andon.name)
		self.assertEqual(frappe.db.get_value("Andon Event", andon.name, "status"), "Raised")
		with as_user(SUPERVISOR):
			andon_service.acknowledge(andon.name)
		self.assertEqual(frappe.db.get_value("Andon Event", andon.name, "status"), "Acknowledged")

		day = add_days(nowdate(), 5)
		portal = make_user("scope.portal@partner.example", [], user_type="Website User")
		for outsider in (self.KYUSHU_SUPERVISOR, portal):
			with self.subTest(outsider), as_user(outsider), self.assertRaises(frappe.PermissionError):
				schedule.build(catalog.LINE, day)
		self.assertFalse(
			frappe.db.exists("Plant Schedule", {"production_line": catalog.LINE, "schedule_date": day})
		)
		with as_user(PLANNER):
			self.assertTrue(schedule.build(catalog.LINE, day))
