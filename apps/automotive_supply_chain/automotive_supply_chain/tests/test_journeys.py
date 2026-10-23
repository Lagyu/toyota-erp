"""Cross-domain business journeys through real ERPNext documents and ledgers:
J-02 Kanban -> call-off -> ASN -> receipt, J-06 invoice match -> dual-approved payment,
J-07 intercompany shipment with balancing AP/AR, J-01 dealer order to cash, J-09 change
after allocation, plus supplier onboarding / sourcing / ASL enforcement."""

from __future__ import annotations

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from automotive_core.governance.approvals import ApprovalRequiredError
from automotive_core.integration_hub import inbound
from automotive_core.tests.utils import AICHI, KYUSHU, approve, as_user
from automotive_manufacturing.mes.production import build_entry
from automotive_manufacturing.tests import catalog
from automotive_supply_chain.tests import helpers as h


class JourneyTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		h.users()
		cls.cat = catalog.build()


class TestKanbanSupplierJourney(JourneyTestCase):
	"""J-02: line consumption -> Kanban signal -> call-off PO -> ASN -> receipt -> replenished."""

	def test_pull_replenishment_end_to_end(self):
		wheel = self.cat["wheel"]
		supplier = h.supplier("Kaminari Electronics")
		bo = h.blanket_order(supplier, wheel, 120)
		loop = frappe.get_doc({"doctype": "Kanban Loop", "loop_code": "KL-SUP-WHEEL", "item_code": wheel,
			"plant": catalog.PLANT, "production_line": catalog.LINE, "supply_type": "Supplier", "supplier": supplier,
			"blanket_order": bo, "line_side_warehouse": catalog.LINE_SIDE, "container_qty": 4, "number_of_cards": 3,
			"lead_time_hours": 4}).insert(ignore_permissions=True)
		catalog.receive(wheel, 12, rate=120)
		issue = frappe.get_doc({"doctype": "Stock Entry", "purpose": "Material Issue", "stock_entry_type": "Material Issue",
			"company": AICHI, "items": [{"item_code": wheel, "qty": 4, "s_warehouse": catalog.LINE_SIDE,
				"use_serial_batch_fields": 1}]})
		issue.insert(ignore_permissions=True)
		issue.submit()
		[signal] = frappe.get_all("Kanban Signal", filters={"kanban_loop": loop.name}, fields=["name", "material_request"])
		self.assertTrue(signal.material_request)
		from automotive_supply_chain.supplier_collaboration.schedules import release_kanban_calloffs

		[po_name] = release_kanban_calloffs()
		po = frappe.get_doc("Purchase Order", po_name)
		self.assertEqual(po.docstatus, 1)  # call-off: no new approval
		self.assertEqual(po.automotive_release_type, "Blanket Call-Off")
		self.assertEqual((po.items[0].blanket_order, flt(po.items[0].rate)), (bo, 120))
		self.assertEqual(frappe.db.get_value("Kanban Signal", signal.name, "status"), "Ordered")
		# supplier system sends the ASN through the integration hub (as the supplier's service user)
		service = h.portal_user("edi.kaminari@kaminari.example", "Supplier", supplier)
		frappe.get_doc({"doctype": "Integration Partner", "partner_code": "KAMINARI", "partner_name": supplier,
			"partner_type": "Supplier", "party_type": "Supplier", "party": supplier, "adapter": "Local File Drop",
			"allowed_message_types": "asn", "service_user": service}).insert(ignore_permissions=True)
		message = {"message_id": "KAM-ASN-0001", "message_type": "asn", "data": {
			"supplier_asn_no": "KAM-0001", "ship_date": f"{nowdate()} 06:00:00",
			"expected_arrival": f"{add_days(nowdate(), 1)} 08:00:00", "carrier": "Raijin Logistics",
			"lines": [{"purchase_order": po.name, "po_item": po.items[0].name, "item_code": wheel, "qty": 4,
				"supplier_lot": "KAM-LOT-77"}]}}
		frappe.flags.automotive_pending_inbound = []
		inbound.accept("KAMINARI", json.dumps(message))
		self.assertEqual(inbound.run_pending_inline(), ["Processed"])
		asn = frappe.get_last_doc("Advance Shipping Notice", filters={"supplier_asn_no": "KAM-0001"})
		self.assertEqual(asn.status, "In Transit")
		from automotive_supply_chain.supplier_collaboration.shipments import make_purchase_receipt

		pr = frappe.get_doc("Purchase Receipt", make_purchase_receipt(asn.name))
		pr.submit()
		self.assertEqual(frappe.db.get_value("Advance Shipping Notice", asn.name, "status"), "Received")
		self.assertEqual(frappe.db.get_value("Kanban Signal", signal.name, "status"), "Received")
		self.assertEqual(frappe.db.count("Kanban Card", {"kanban_loop": loop.name, "status": "Full"}), 3)
		bin_ = frappe.db.get_value("Bin", {"item_code": wheel, "warehouse": catalog.LINE_SIDE},
			["actual_qty", "valuation_rate"], as_dict=True)
		self.assertEqual(flt(bin_.actual_qty), 12)
		self.assertAlmostEqual(flt(bin_.valuation_rate), 120, places=2)
		batch = frappe.db.get_value("Purchase Receipt Item", {"parent": pr.name}, "batch_no")
		self.assertEqual(frappe.db.get_value("Batch", batch, "automotive_supplier_lot"), "KAM-LOT-77")
		self.assertEqual(flt(frappe.db.get_value("Purchase Order", po.name, "per_received")), 100)


class TestInvoiceMatchAndPayment(JourneyTestCase):
	"""J-06: supplier invoice -> PO/receipt match -> exception -> correction -> dual approval -> settlement."""

	def test_three_way_match_and_dual_approved_payment(self):
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

		from automotive_core.governance.approvals import request_approval
		from automotive_supply_chain.procurement_controls import three_way_match

		wheel = self.cat["wheel"]
		supplier = h.supplier("Hoshi Metalworks")
		with as_user(h.BUYER):
			po = frappe.get_doc({"doctype": "Purchase Order", "supplier": supplier, "company": AICHI,
				"transaction_date": nowdate(), "schedule_date": add_days(nowdate(), 5), "items": [{
					"item_code": wheel, "qty": 10, "rate": 120, "warehouse": catalog.PARTS_STORE,
					"schedule_date": add_days(nowdate(), 5)}]}).insert()
			with self.assertRaises(ApprovalRequiredError):
				po.submit()
		approve(po, h.BUYER, [h.PROC_MGR])
		with as_user(h.BUYER):
			po.reload()
			po.submit()
		pr = frappe.get_doc(make_purchase_receipt(po.name))
		pr.insert(ignore_permissions=True)
		pr.submit()
		with as_user(h.AP_CLERK):
			pi = frappe.get_doc(make_purchase_invoice(pr.name))
			pi.items[0].qty = 12
			pi.items[0].rate = 126
			pi.insert()
		pi.reload()
		self.assertEqual(pi.automotive_match_status, "Exceptions Open")
		types = set(frappe.get_all("Invoice Match Exception", filters={"purchase_invoice": pi.name},
			pluck="exception_type"))
		self.assertEqual(types, {"Quantity Over Accepted", "Price Variance"})
		with as_user(h.AP_CLERK), self.assertRaises(three_way_match.MatchExceptionError):
			pi.submit()
		qty_exc = frappe.db.get_value("Invoice Match Exception", {"purchase_invoice": pi.name,
			"exception_type": "Quantity Over Accepted"}, "name")
		with as_user(h.PROC_MGR), self.assertRaises(frappe.ValidationError):
			three_way_match.accept_exception(qty_exc, "cannot accept quantity")
		with as_user(h.PROC_MGR):
			three_way_match.reject_exception(qty_exc, "Supplier must bill the 10 received")
		# AP corrects the invoice to the received quantity and the order price
		with as_user(h.AP_CLERK):
			pi.reload()
			pi.items[0].qty = 10
			pi.items[0].rate = 120
			pi.save()
			pi.reload()
			self.assertEqual(pi.automotive_match_status, "Matched")
			pi.submit()
		statuses = set(frappe.get_all("Invoice Match Exception", filters={"purchase_invoice": pi.name}, pluck="status"))
		self.assertEqual(statuses, {"Corrected"})
		# payment: verified supplier bank account + two different treasury approvers
		bank_gl = h.company_bank_account(AICHI)
		party_bank = h.verified_supplier_bank_account(supplier)
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		with as_user(h.AP_CLERK):
			pe = get_payment_entry("Purchase Invoice", pi.name)
			pe.mode_of_payment = "Wire Transfer"
			pe.paid_from = bank_gl
			pe.party_bank_account = party_bank
			pe.reference_no = "WIRE-0001"
			pe.reference_date = nowdate()
			pe.insert()
			with self.assertRaises(ApprovalRequiredError):
				pe.submit()
			request_approval(pe.doctype, pe.name)
		from automotive_core.governance.approvals import ApprovalAuthorityError, decide

		with as_user(h.TREASURY1):
			req = frappe.get_last_doc("Approval Request", filters={"reference_name": pe.name}).name
			decide(req, "Approve")
			with self.assertRaises(ApprovalAuthorityError):
				decide(req, "Approve")  # dual approval needs a second person
		with as_user(h.TREASURY2):
			self.assertEqual(decide(req, "Approve")["status"], "Approved")
		with as_user(h.AP_CLERK):
			pe.reload()
			pe.submit()
		self.assertEqual(flt(frappe.db.get_value("Purchase Invoice", pi.name, "outstanding_amount")), 0)
		events = set(frappe.get_all("Audit Event", filters={"reference_name": pe.name}, pluck="event_type"))
		self.assertTrue({"approval.requested", "approval.approve", "document.submitted"} <= events)


class TestIntercompanyJourney(JourneyTestCase):
	"""J-07: Kyushu ships components to Aichi; both companies book matching stock and AR/AP."""

	def test_intercompany_shipment_balances(self):
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_inter_company_purchase_invoice
		from erpnext.buying.doctype.purchase_order.purchase_order import make_inter_company_sales_order
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
		from erpnext.stock.doctype.delivery_note.delivery_note import (
			make_inter_company_purchase_receipt,
			make_sales_invoice,
		)

		from automotive_supply_chain.intercompany.transfer_pricing import reconciliation

		inverter = "TST-INVERTER"
		if not frappe.db.exists("Item", inverter):
			frappe.get_doc({"doctype": "Item", "item_code": inverter, "item_name": "Test Inverter", "item_group":
				"Sub Assemblies", "stock_uom": "Nos", "is_stock_item": 1, "valuation_rate": 50_000,
				"item_defaults": [{"company": KYUSHU, "default_warehouse": h.KYUSHU_FG},
					{"company": AICHI, "default_warehouse": catalog.PARTS_STORE}]}).insert(ignore_permissions=True)
		receipt = frappe.get_doc({"doctype": "Stock Entry", "purpose": "Material Receipt",
			"stock_entry_type": "Material Receipt", "company": KYUSHU,
			"items": [{"item_code": inverter, "qty": 5, "t_warehouse": h.KYUSHU_FG, "basic_rate": 50_000}]})
		receipt.insert(ignore_permissions=True)
		receipt.submit()
		h.transfer_price(KYUSHU, AICHI, inverter, 60_000)
		internal_supplier = frappe.db.get_value("Supplier", {"is_internal_supplier": 1, "represents_company": KYUSHU}, "name")
		with as_user(h.BUYER):
			po = frappe.get_doc({"doctype": "Purchase Order", "supplier": internal_supplier, "company": AICHI,
				"buying_price_list": "Intercompany JPY",
				"transaction_date": nowdate(), "schedule_date": add_days(nowdate(), 3),
				"items": [{"item_code": inverter, "qty": 5, "rate": 59_000, "warehouse": catalog.PARTS_STORE,
					"schedule_date": add_days(nowdate(), 3)}]})
			with self.assertRaises(frappe.ValidationError):  # off the agreed transfer price
				po.insert()
			po.items[0].rate = 60_000
			po.insert()
		approve(po, h.BUYER, [h.PROC_MGR])
		with as_user(h.BUYER):
			po.reload()
			po.submit()
		so = make_inter_company_sales_order(po.name)
		so.delivery_date = add_days(nowdate(), 2)
		for item in so.items:
			item.warehouse = h.KYUSHU_FG
			item.delivery_date = so.delivery_date
		so.insert(ignore_permissions=True)
		so.submit()
		dn = make_delivery_note(so.name)
		dn.insert(ignore_permissions=True)
		dn.submit()
		pr = make_inter_company_purchase_receipt(dn.name)
		for item in pr.items:
			item.warehouse = catalog.PARTS_STORE
		pr.insert(ignore_permissions=True)
		pr.submit()
		si = make_sales_invoice(dn.name)
		si.insert(ignore_permissions=True)
		si.submit()
		pi = make_inter_company_purchase_invoice(si.name)
		pi.insert(ignore_permissions=True)
		pi.reload()
		self.assertEqual(pi.automotive_match_status, "Matched")
		pi.submit()
		result = reconciliation(KYUSHU, AICHI)
		self.assertTrue(result["balanced"], result)
		self.assertEqual(flt(frappe.db.get_value("Bin", {"item_code": inverter, "warehouse": catalog.PARTS_STORE},
			"actual_qty")), 5)
		self.assertEqual(flt(frappe.db.get_value("Bin", {"item_code": inverter, "warehouse": h.KYUSHU_FG}, "actual_qty")), 0)


class TestDealerJourney(JourneyTestCase):
	"""J-01 order to cash for one vehicle, J-09 cancellation after allocation."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.variant = cls.cat["variants"][("Premium", "AWD")]
		h.item_price(cls.variant, "Dealer Price JP", 2_500_000)
		registration = frappe.get_doc({"doctype": "Dealer Registration", "dealer_name": "Asahi Motors Nagoya",
			"dealer_code": "ASAHI-NGY", "company": AICHI, "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			"country": "Japan", "tax_id": "JP-T5550001", "requested_credit_limit": 100_000_000,
			"price_list": "Dealer Price JP", "authorized_models": [{"vehicle_model": catalog.MODEL}],
			"contacts": [{"first_name": "Mei", "last_name": "Asahi", "email": "orders@asahi-motors.example",
				"partner_role": "Sales"}]})
		registration.insert(ignore_permissions=True)
		approve(registration, h.LIAISON, [h.SALES_MGR, h.CONTROLLER])
		registration.reload()
		registration.submit()
		cls.dealer = frappe.db.get_value("Dealer Registration", registration.name, "customer")
		cls.portal = "orders@asahi-motors.example"

	def _order(self, reference: str) -> str:
		from automotive_manufacturing.product_engineering.configuration import configure

		with as_user(self.portal):
			config = configure(catalog.MODEL, {"Trim": "Premium", "Drivetrain": "AWD"})["configuration"]
			order = frappe.get_doc({"doctype": "Dealer Order", "dealer": self.dealer, "dealer_reference": reference,
				"company": AICHI, "requested_delivery_date": add_days(nowdate(), 30),
				"items": [{"vehicle_model": catalog.MODEL, "configuration": config, "qty": 1, "discount_percent": 10}]})
			order.insert()
			order.submit()
		self.assertEqual(order.items[0].discount_percent, 0)  # dealers cannot self-discount
		from automotive_supply_chain.dealer_operations.orders import accept

		with as_user(h.LIAISON):
			result = accept(order.name)
		self.assertFalse(result["needs_approval"])
		return frappe.db.get_value("Dealer Order", order.name, "sales_order")

	def _released_vehicle(self) -> str:
		cat = self.cat
		for item, qty in ((cat["wheel"], 4), (cat["seat"], 2)):
			if frappe.get_cached_value("Item", item, "has_batch_no"):
				batch = catalog.make_batch(item, f"B-{frappe.generate_hash(length=8)}")
				catalog.receive(item, qty, batch_no=batch)
			else:
				catalog.receive(item, qty)
		ecu = f"TECU-{frappe.generate_hash(length=8).upper()}"
		catalog.receive(cat["ecu"], 1, serial_nos=[ecu], rate=900)
		wo = catalog.work_order(self.variant, 1)
		se = build_entry(wo.name, 1)
		vin = frappe.get_all("Vehicle Unit", filters={"work_order": wo.name}, pluck="name")[0]
		qi = frappe.get_doc({"doctype": "Quality Inspection", "inspection_type": "In Process",
			"automotive_inspection_stage": "Final", "reference_type": "Stock Entry", "reference_name": se.name,
			"item_code": self.variant, "item_serial_no": vin, "sample_size": 1, "inspected_by": "Administrator",
			"status": "Accepted", "manual_inspection": 1, "company": AICHI})
		qi.insert(ignore_permissions=True)
		qi.submit()
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "quality_status"), "Released")
		return vin

	def test_order_to_cash(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		from automotive_supply_chain.dealer_operations.allocation import allocate
		from automotive_supply_chain.logistics.operations import record_delivery

		so = self._order("ASAHI-PO-1001")
		self._released_vehicle()
		allocate(AICHI)
		# FIFO: the oldest released vehicle of the variant serves the order
		vin = frappe.db.get_value("Vehicle Allocation", {"sales_order": so, "status": "Allocated"}, "vehicle_unit")
		self.assertTrue(vin)
		unit = frappe.get_doc("Vehicle Unit", vin)
		self.assertEqual((unit.inventory_status, unit.dealer, unit.sales_order), ("Reserved", self.dealer, so))
		sre = frappe.db.get_value("Vehicle Allocation", unit.automotive_allocation, "stock_reservation_entry")
		self.assertEqual(frappe.db.get_value("Stock Reservation Entry", sre, "docstatus"), 1)
		dn = make_delivery_note(so)
		for item in dn.items:
			item.use_serial_batch_fields = 1
			item.serial_no = vin
		dn.insert(ignore_permissions=True)
		dn.submit()
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "inventory_status"), "Delivered")
		carrier = h.supplier("Raijin Logistics", group="Carriers")
		with as_user(h.LOGISTICS):
			booking = frappe.get_doc({"doctype": "Transport Booking", "carrier": carrier, "company": AICHI,
				"plant": catalog.PLANT, "direction": "Outbound", "vehicle_type": "Car Carrier",
				"pickup_window_start": f"{nowdate()} 09:00:00", "pickup_window_end": f"{nowdate()} 12:00:00",
				"delivery_window_end": f"{add_days(nowdate(), 1)} 18:00:00", "destination": "Asahi Motors Nagoya"}).insert()
			manifest = frappe.get_doc({"doctype": "Shipment Manifest", "company": AICHI, "plant": catalog.PLANT,
				"transport_booking": booking.name, "carrier": carrier, "departure": f"{nowdate()} 10:00:00",
				"seal_no": "SEAL-881", "items": [{"delivery_note": dn.name}]}).insert()
			manifest.submit()
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "shipment_status"), "Dispatched")
		with as_user(h.LOGISTICS):
			record_delivery(manifest.name, "Taro Yard-Manager")
		with as_user(self.portal):
			acceptance = frappe.get_doc({"doctype": "Delivery Acceptance", "delivery_note": dn.name,
				"condition": "Accepted"}).insert()
			acceptance.submit()
		unit.reload()
		self.assertEqual(unit.shipment_status, "Accepted")
		self.assertTrue(unit.warranty_start_date and unit.warranty_end_date)
		si = make_sales_invoice(dn.name)
		si.insert(ignore_permissions=True)
		si.submit()
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "invoice_status"), "Invoiced")
		pe = get_payment_entry("Sales Invoice", si.name)
		pe.reference_no, pe.reference_date = "DEALER-REMIT-1", nowdate()
		pe.insert(ignore_permissions=True)
		pe.submit()
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "invoice_status"), "Paid")
		# ledgers: revenue and receivable booked, receivable cleared
		gl = frappe.db.sql("""select sum(debit) - sum(credit) from `tabGL Entry` where voucher_no in (%s, %s)
			and party = %s and is_cancelled = 0""", (si.name, pe.name, self.dealer))[0][0]
		self.assertAlmostEqual(flt(gl), 0, places=2)

	def test_cancellation_after_allocation_releases_reservation(self):
		from automotive_supply_chain.dealer_operations.allocation import allocate

		so = self._order("ASAHI-PO-1002")
		self._released_vehicle()
		allocate(AICHI)
		allocation = frappe.db.get_value("Vehicle Allocation", {"sales_order": so, "status": "Allocated"}, "name")
		vin = frappe.db.get_value("Vehicle Allocation", allocation, "vehicle_unit")
		sre = frappe.db.get_value("Vehicle Allocation", allocation, "stock_reservation_entry")
		value_before = flt(frappe.db.get_value("Bin", {"item_code": self.variant, "warehouse": catalog.FG}, "stock_value"))
		order = frappe.db.get_value("Sales Order", so, "automotive_dealer_order")
		with as_user(self.portal):
			change = frappe.get_doc({"doctype": "Dealer Order Change", "dealer_order": order, "change_type": "Cancel Order",
				"reason": "End customer withdrew"}).insert()
			change.submit()
		change.reload()
		self.assertEqual(change.status, "Applied")
		self.assertEqual(frappe.db.get_value("Sales Order", so, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Stock Reservation Entry", sre, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Vehicle Allocation", allocation, "status"), "Released")
		unit = frappe.get_doc("Vehicle Unit", vin)
		self.assertEqual((unit.inventory_status, unit.dealer, unit.automotive_allocation), ("In Stock", None, None))
		value_after = flt(frappe.db.get_value("Bin", {"item_code": self.variant, "warehouse": catalog.FG}, "stock_value"))
		self.assertAlmostEqual(value_before, value_after, places=2)
		self.assertTrue(frappe.db.exists("Audit Event", {"event_type": "allocation.released", "reference_name": allocation}))


class TestSourcing(JourneyTestCase):
	def test_onboarding_sourcing_and_asl_enforcement(self):
		from automotive_supply_chain.supplier_collaboration.sourcing import approved_entries

		with as_user(h.BUYER):
			onboarding = frappe.get_doc({"doctype": "Supplier Onboarding", "supplier_name": "Tsukimi Castings",
				"partner_code": "TSUKIMI", "supplier_group": "Production Material", "country": "Japan",
				"tax_id": "JP-T7770001", "risk_rating": "Low",
				"certifications": [{"standard": "IATF 16949", "certificate_no": "IATF-123", "valid_until": add_days(nowdate(), 400)}],
				"contacts": [{"first_name": "Ren", "email": "ren@tsukimi.example", "partner_role": "Sales"}],
				"proposed_items": [{"item_code": self.cat["wheel"], "plant": catalog.PLANT}]}).insert()
		approve(onboarding, h.BUYER, [h.PROC_MGR, h.QM])
		with as_user(h.BUYER):
			onboarding.reload()
			onboarding.submit()
		supplier = frappe.db.get_value("Supplier Onboarding", onboarding.name, "supplier")
		self.assertTrue(frappe.db.exists("Partner User Access", {"user": "ren@tsukimi.example", "supplier": supplier}))
		self.assertEqual(approved_entries(self.cat["wheel"], catalog.PLANT), [])  # provisional only
		with as_user(h.BUYER):
			decision = frappe.get_doc({"doctype": "Sourcing Decision", "company": AICHI, "plant": catalog.PLANT,
				"item_code": self.cat["wheel"], "effective_from": nowdate(), "rationale": "Best value",
				"quotes": [{"supplier": supplier, "unit_price": 115, "lead_time_days": 3, "quality_score": 95},
					{"supplier": h.supplier("Kaminari Electronics"), "unit_price": 120, "lead_time_days": 3,
						"quality_score": 90}]}).insert()
		self.assertEqual(decision.awarded_supplier, supplier)
		approve(decision, h.BUYER, [h.PROC_MGR])
		with as_user(h.BUYER):
			decision.reload()
			decision.submit()
		self.assertEqual(approved_entries(self.cat["wheel"], catalog.PLANT)[0].supplier, supplier)
		settings = frappe.get_single("Supply Chain Settings")
		settings.set("asl_item_groups", [{"item_group": "Raw Material"}])
		settings.save(ignore_permissions=True)
		frappe.clear_document_cache("Supply Chain Settings", "Supply Chain Settings")
		with as_user(h.BUYER), self.assertRaises(frappe.ValidationError):
			frappe.get_doc({"doctype": "Purchase Order", "supplier": h.supplier("Hoshi Metalworks"), "company": AICHI,
				"transaction_date": nowdate(), "schedule_date": add_days(nowdate(), 5), "automotive_plant": catalog.PLANT,
				"items": [{"item_code": self.cat["wheel"], "qty": 1, "rate": 120, "warehouse": catalog.PARTS_STORE,
					"schedule_date": add_days(nowdate(), 5)}]}).insert()
