"""Cross-domain business journeys through real ERPNext documents and ledgers:
J-02 Kanban -> call-off -> ASN -> receipt (and receipt discrepancies), J-06 invoice match ->
dual-approved payment, J-07 intercompany shipment with balancing AP/AR, J-01 dealer order to
cash (credit limit, credit note), J-09 change after allocation, plus supplier onboarding /
sourcing / ASL enforcement."""

from __future__ import annotations

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from automotive_core.governance.approvals import ApprovalRequiredError
from automotive_core.integration_hub import inbound
from automotive_core.tests.utils import AICHI, KYUSHU, NA, approve, as_user, make_user
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
		loop = frappe.get_doc(
			{
				"doctype": "Kanban Loop",
				"loop_code": "KL-SUP-WHEEL",
				"item_code": wheel,
				"plant": catalog.PLANT,
				"production_line": catalog.LINE,
				"supply_type": "Supplier",
				"supplier": supplier,
				"blanket_order": bo,
				"line_side_warehouse": catalog.LINE_SIDE,
				"container_qty": 4,
				"number_of_cards": 3,
				"lead_time_hours": 4,
			}
		).insert(ignore_permissions=True)
		catalog.receive(wheel, 12, rate=120)
		issue = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Material Issue",
				"stock_entry_type": "Material Issue",
				"company": AICHI,
				"items": [
					{
						"item_code": wheel,
						"qty": 4,
						"s_warehouse": catalog.LINE_SIDE,
						"use_serial_batch_fields": 1,
					}
				],
			}
		)
		issue.insert(ignore_permissions=True)
		issue.submit()
		[signal] = frappe.get_all(
			"Kanban Signal", filters={"kanban_loop": loop.name}, fields=["name", "material_request"]
		)
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
		frappe.get_doc(
			{
				"doctype": "Integration Partner",
				"partner_code": "KAMINARI",
				"partner_name": supplier,
				"partner_type": "Supplier",
				"party_type": "Supplier",
				"party": supplier,
				"adapter": "Local File Drop",
				"allowed_message_types": "asn",
				"service_user": service,
			}
		).insert(ignore_permissions=True)
		message = {
			"message_id": "KAM-ASN-0001",
			"message_type": "asn",
			"data": {
				"supplier_asn_no": "KAM-0001",
				"ship_date": f"{nowdate()} 06:00:00",
				"expected_arrival": f"{add_days(nowdate(), 1)} 08:00:00",
				"carrier": "Raijin Logistics",
				"lines": [
					{
						"purchase_order": po.name,
						"po_item": po.items[0].name,
						"item_code": wheel,
						"qty": 4,
						"supplier_lot": "KAM-LOT-77",
					}
				],
			},
		}
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
		bin_ = frappe.db.get_value(
			"Bin",
			{"item_code": wheel, "warehouse": catalog.LINE_SIDE},
			["actual_qty", "valuation_rate"],
			as_dict=True,
		)
		self.assertEqual(flt(bin_.actual_qty), 12)
		self.assertAlmostEqual(flt(bin_.valuation_rate), 120, places=2)
		batch = frappe.db.get_value("Purchase Receipt Item", {"parent": pr.name}, "batch_no")
		self.assertEqual(frappe.db.get_value("Batch", batch, "automotive_supplier_lot"), "KAM-LOT-77")
		self.assertEqual(flt(frappe.db.get_value("Purchase Order", po.name, "per_received")), 100)


class TestReceiptDiscrepancy(JourneyTestCase):
	"""ERP-05/06: goods received short of their ASN open a discrepancy the supplier is told
	about; procurement resolves it, once."""

	def test_a_short_receipt_opens_a_discrepancy_that_procurement_resolves(self):
		from automotive_supply_chain.supplier_collaboration.shipments import (
			make_purchase_receipt,
			resolve_discrepancy,
		)

		wheel = self.cat["wheel"]
		supplier = h.supplier("Hoshi Metalworks")
		due = add_days(nowdate(), 5)
		with as_user(h.BUYER):
			po = frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"supplier": supplier,
					"company": AICHI,
					"transaction_date": nowdate(),
					"schedule_date": due,
					"items": [
						{
							"item_code": wheel,
							"qty": 10,
							"rate": 120,
							"warehouse": catalog.PARTS_STORE,
							"schedule_date": due,
						}
					],
				}
			).insert()
		approve(po, h.BUYER, [h.PROC_MGR])
		with as_user(h.BUYER):
			po.reload()
			po.submit()
		asn = frappe.get_doc(
			{
				"doctype": "Advance Shipping Notice",
				"supplier": supplier,
				"supplier_asn_no": "HOSHI-0001",
				"company": AICHI,
				"ship_date": f"{nowdate()} 06:00:00",
				"expected_arrival": f"{add_days(nowdate(), 1)} 08:00:00",
				"items": [
					{
						"purchase_order": po.name,
						"po_item": po.items[0].name,
						"item_code": wheel,
						"qty": 10,
						"supplier_lot": "HOSHI-LOT-7",
					}
				],
			}
		)
		asn.insert(ignore_permissions=True)
		asn.submit()
		pr = frappe.get_doc("Purchase Receipt", make_purchase_receipt(asn.name))
		self.assertEqual(flt(pr.items[0].qty), 10)  # pre-filled with what the supplier announced
		pr.items[0].qty = pr.items[0].received_qty = 8  # the truck brought two wheels less
		pr.submit()
		[disc] = frappe.get_all(
			"Receipt Discrepancy",
			filters={"advance_shipping_notice": asn.name},
			fields=[
				"name",
				"purchase_receipt",
				"discrepancy_type",
				"expected_qty",
				"received_qty",
				"rejected_qty",
				"status",
			],
		)
		self.assertEqual(
			(disc.purchase_receipt, disc.discrepancy_type, disc.status), (pr.name, "Shortage", "Open")
		)
		self.assertEqual((flt(disc.expected_qty), flt(disc.received_qty), flt(disc.rejected_qty)), (10, 8, 0))
		self.assertEqual(
			frappe.db.get_value("Advance Shipping Notice", asn.name, "status"), "Partially Received"
		)
		self.assertEqual(flt(frappe.db.get_value("ASN Item", asn.items[0].name, "received_qty")), 8)
		self.assertEqual(
			frappe.db.get_value(
				"Supplier Notice",
				{"reference_doctype": "Receipt Discrepancy", "reference_name": disc.name},
				"supplier",
			),
			supplier,
		)  # shown in the supplier's portal
		with as_user(h.QM), self.assertRaises(frappe.PermissionError):
			resolve_discrepancy(disc.name, "Accept As Received", "Quality does not settle quantities")
		with as_user(h.BUYER):
			self.assertEqual(
				resolve_discrepancy(
					disc.name, "Supplier Ships Balance", "The two wheels follow on the next truck"
				),
				"Resolved",
			)
		resolved = frappe.db.get_value(
			"Receipt Discrepancy",
			disc.name,
			["status", "resolution", "resolved_by", "resolved_at"],
			as_dict=True,
		)
		self.assertEqual(
			(resolved.status, resolved.resolution, resolved.resolved_by),
			("Resolved", "Supplier Ships Balance", h.BUYER),
		)
		self.assertTrue(resolved.resolved_at)
		with as_user(h.BUYER), self.assertRaises(frappe.ValidationError):
			resolve_discrepancy(disc.name, "Debit Note", "A second decision on the same shortfall")


class TestDeliverySchedule(JourneyTestCase):
	def test_firm_lines_order_each_request_line_once(self):
		"""A request may hold several lines of one item (planning runs do); each firm schedule
		line calls off against its own request line."""
		wheel = self.cat["wheel"]
		supplier = h.supplier("Hoshi Metalworks")
		h.blanket_order(supplier, wheel, 118)
		mr = frappe.get_doc(
			{
				"doctype": "Material Request",
				"material_request_type": "Purchase",
				"company": AICHI,
				"schedule_date": add_days(nowdate(), 3),
				"items": [
					{
						"item_code": wheel,
						"qty": 4,
						"schedule_date": add_days(nowdate(), 3),
						"warehouse": catalog.PARTS_STORE,
					},
					{
						"item_code": wheel,
						"qty": 6,
						"schedule_date": add_days(nowdate(), 5),
						"warehouse": catalog.PARTS_STORE,
					},
				],
			}
		)
		mr.insert(ignore_permissions=True)
		mr.submit()
		schedule = frappe.get_doc(
			{
				"doctype": "Supplier Delivery Schedule",
				"supplier": supplier,
				"company": AICHI,
				"plant": catalog.PLANT,
				"horizon_start": nowdate(),
				"horizon_end": add_days(nowdate(), 30),
				"firm_days": 10,
				"lines": [
					{
						"item_code": wheel,
						"delivery_date": r.schedule_date,
						"qty": r.qty,
						"firm": 1,
						"warehouse": r.warehouse,
						"material_request": mr.name,
						"material_request_item": r.name,
					}
					for r in mr.items
				],
			}
		)
		schedule.insert(ignore_permissions=True)
		schedule.submit()
		po = frappe.get_doc(
			"Purchase Order",
			frappe.db.get_value("Supplier Schedule Line", {"parent": schedule.name}, "purchase_order"),
		)
		self.assertEqual(
			sorted((r.material_request_item, flt(r.qty)) for r in po.items),
			sorted((r.name, flt(r.qty)) for r in mr.items),
		)
		self.assertEqual(flt(frappe.db.get_value("Material Request", mr.name, "per_ordered")), 100)


class TestInvoiceMatchAndPayment(JourneyTestCase):
	"""J-06: supplier invoice -> PO/receipt match -> exception -> correction -> dual approval -> settlement."""

	def test_a_supplier_invoice_by_message_becomes_a_draft_the_match_checks(self):
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

		from automotive_supply_chain.integrations import handlers

		wheel = self.cat["wheel"]
		supplier = h.supplier("Hoshi Metalworks")
		with as_user(h.BUYER):
			po = frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"supplier": supplier,
					"company": AICHI,
					"transaction_date": nowdate(),
					"schedule_date": add_days(nowdate(), 5),
					"items": [
						{
							"item_code": wheel,
							"qty": 10,
							"rate": 120,
							"warehouse": catalog.PARTS_STORE,
							"schedule_date": add_days(nowdate(), 5),
						}
					],
				}
			).insert()
		approve(po, h.BUYER, [h.PROC_MGR])
		with as_user(h.BUYER):
			po.reload()
			po.submit()
		pr = frappe.get_doc(make_purchase_receipt(po.name))
		pr.insert(ignore_permissions=True)
		pr.submit()
		partner = frappe.get_doc(
			{
				"doctype": "Integration Partner",
				"partner_code": "HOSHI-INV",
				"partner_name": "Hoshi invoicing",
				"partner_type": "Supplier",
				"party_type": "Supplier",
				"party": supplier,
				"adapter": "Local File Drop",
				"allowed_message_types": "supplier_invoice",
				"service_user": h.AP_CLERK,
			}
		).insert(ignore_permissions=True)
		invoice = {
			"invoice_no": "HM-INV-7781",
			"invoice_date": nowdate(),
			"purchase_receipt": pr.name,
			"lines": [{"item_code": wheel, "qty": 10, "rate": 126}],
		}
		doctype, name = handlers.supplier_invoice(None, partner, invoice)
		pi = frappe.get_doc(doctype, name)
		self.assertEqual((pi.docstatus, pi.supplier, pi.bill_no), (0, supplier, "HM-INV-7781"))
		self.assertEqual([(r.item_code, flt(r.qty), flt(r.rate)) for r in pi.items], [(wheel, 10, 126)])
		# checked like an invoice typed in: the price is above the order's
		self.assertEqual(pi.automotive_match_status, "Exceptions Open")
		self.assertEqual(
			set(
				frappe.get_all(
					"Invoice Match Exception", filters={"purchase_invoice": pi.name}, pluck="exception_type"
				)
			),
			{"Price Variance"},
		)
		self.assertEqual(handlers.supplier_invoice(None, partner, invoice), (doctype, name))  # resent
		with self.assertRaises(handlers.InboundRejected):  # an item the receipt does not hold
			handlers.supplier_invoice(
				None,
				partner,
				{
					**invoice,
					"invoice_no": "HM-INV-7782",
					"lines": [{"item_code": self.cat["seat"], "qty": 1, "rate": 10}],
				},
			)
		other = frappe.get_doc(
			{
				"doctype": "Integration Partner",
				"partner_code": "OTHER-INV",
				"partner_name": "Another supplier's invoicing",
				"partner_type": "Supplier",
				"party_type": "Supplier",
				"party": h.supplier("Kaze Seating"),
				"adapter": "Local File Drop",
				"allowed_message_types": "supplier_invoice",
				"service_user": h.AP_CLERK,
			}
		).insert(ignore_permissions=True)
		with self.assertRaises(handlers.InboundRejected):  # another supplier's receipt
			handlers.supplier_invoice(None, other, {**invoice, "invoice_no": "KZ-1"})

	def test_three_way_match_and_dual_approved_payment(self):
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

		from automotive_core.governance.approvals import request_approval
		from automotive_supply_chain.procurement_controls import three_way_match

		wheel = self.cat["wheel"]
		supplier = h.supplier("Hoshi Metalworks")
		with as_user(h.BUYER):
			po = frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"supplier": supplier,
					"company": AICHI,
					"transaction_date": nowdate(),
					"schedule_date": add_days(nowdate(), 5),
					"items": [
						{
							"item_code": wheel,
							"qty": 10,
							"rate": 120,
							"warehouse": catalog.PARTS_STORE,
							"schedule_date": add_days(nowdate(), 5),
						}
					],
				}
			).insert()
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
		types = set(
			frappe.get_all(
				"Invoice Match Exception", filters={"purchase_invoice": pi.name}, pluck="exception_type"
			)
		)
		self.assertEqual(types, {"Quantity Over Accepted", "Price Variance"})
		with as_user(h.AP_CLERK), self.assertRaises(three_way_match.MatchExceptionError):
			pi.submit()
		qty_exc = frappe.db.get_value(
			"Invoice Match Exception",
			{"purchase_invoice": pi.name, "exception_type": "Quantity Over Accepted"},
			"name",
		)
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
		statuses = set(
			frappe.get_all("Invoice Match Exception", filters={"purchase_invoice": pi.name}, pluck="status")
		)
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
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		from automotive_supply_chain.intercompany.transfer_pricing import reconciliation

		inverter = "TST-INVERTER"
		if not frappe.db.exists("Item", inverter):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": inverter,
					"item_name": "Test Inverter",
					"item_group": "Sub Assemblies",
					"stock_uom": "Nos",
					"is_stock_item": 1,
					"valuation_rate": 50_000,
					"item_defaults": [
						{"company": KYUSHU, "default_warehouse": h.KYUSHU_FG},
						{"company": AICHI, "default_warehouse": catalog.PARTS_STORE},
					],
				}
			).insert(ignore_permissions=True)
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Material Receipt",
				"stock_entry_type": "Material Receipt",
				"company": KYUSHU,
				"items": [
					{"item_code": inverter, "qty": 5, "t_warehouse": h.KYUSHU_FG, "basic_rate": 50_000}
				],
			}
		)
		receipt.insert(ignore_permissions=True)
		receipt.submit()
		h.transfer_price(KYUSHU, AICHI, inverter, 60_000)
		internal_supplier = frappe.db.get_value(
			"Supplier", {"is_internal_supplier": 1, "represents_company": KYUSHU}, "name"
		)
		with as_user(h.BUYER):
			po = frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"supplier": internal_supplier,
					"company": AICHI,
					"buying_price_list": "Intercompany JPY",
					"transaction_date": nowdate(),
					"schedule_date": add_days(nowdate(), 3),
					"items": [
						{
							"item_code": inverter,
							"qty": 5,
							"rate": 59_000,
							"warehouse": catalog.PARTS_STORE,
							"schedule_date": add_days(nowdate(), 3),
						}
					],
				}
			)
			with self.assertRaises(frappe.ValidationError):  # off the agreed transfer price
				po.insert()
			po.items[0].rate = 60_000
			po.insert()
		approve(po, h.BUYER, [h.PROC_MGR])
		with as_user(h.BUYER):
			po.reload()
			po.submit()
		from automotive_supply_chain.intercompany.handoff import create_buyer_receipt, create_seller_order

		with as_user(h.SALES_MGR):  # the seller's desk may not read Aichi's PO; the mapping is a system step
			so = frappe.get_doc(
				"Sales Order", create_seller_order(po.name, add_days(nowdate(), 2), h.KYUSHU_FG)
			)
			self.assertEqual(create_seller_order(po.name), so.name)  # idempotent
			so.submit()
		self.assertEqual(
			(so.owner, so.company, so.inter_company_order_reference), (h.SALES_MGR, KYUSHU, po.name)
		)
		self.assertTrue(
			frappe.db.exists(
				"Audit Event",
				{"event_type": "system.on_behalf_of", "reference_name": po.name, "on_behalf_of": h.SALES_MGR},
			)
		)
		external = frappe.get_doc(
			{
				"doctype": "Purchase Order",
				"supplier": h.supplier("Hoshi Metalworks"),
				"company": AICHI,
				"transaction_date": nowdate(),
				"schedule_date": add_days(nowdate(), 3),
				"items": [
					{
						"item_code": self.cat["wheel"],
						"qty": 1,
						"rate": 120,
						"warehouse": catalog.PARTS_STORE,
						"schedule_date": add_days(nowdate(), 3),
					}
				],
			}
		).insert(ignore_permissions=True)
		approve(external, h.BUYER, [h.PROC_MGR])
		external.reload()
		external.submit()
		with as_user(h.SALES_MGR), self.assertRaises(frappe.ValidationError):
			create_seller_order(external.name)
		dn = make_delivery_note(so.name)
		dn.insert(ignore_permissions=True)
		dn.submit()
		from automotive_supply_chain.intercompany.report.goods_in_transit.goods_in_transit import in_transit

		shipped = {r.delivery_note: r for r in in_transit(nowdate())}
		self.assertEqual((shipped[dn.name].buyer, flt(shipped[dn.name].value)), (AICHI, 300_000))
		receiver = "receiving.aichi.sc@hinode.example"
		make_user(receiver, ["Warehouse Clerk", "Stock User"])
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": receiver,
				"allow": "Company",
				"for_value": AICHI,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)
		with as_user(receiver):  # Aichi-scoped clerk: Kyushu's delivery note is out of their scope
			with self.assertRaises(frappe.PermissionError):
				frappe.get_doc("Delivery Note", dn.name).check_permission("read")
			pr = frappe.get_doc("Purchase Receipt", create_buyer_receipt(dn.name, catalog.PARTS_STORE))
			pr.submit()
		self.assertEqual((pr.owner, pr.company), (receiver, AICHI))
		self.assertNotIn(
			dn.name, [r.delivery_note for r in in_transit(nowdate())]
		)  # received: no longer in transit
		si = make_sales_invoice(dn.name)
		si.insert(ignore_permissions=True)
		si.submit()
		pi = make_inter_company_purchase_invoice(si.name)
		pi.insert(ignore_permissions=True)
		pi.reload()
		self.assertEqual(pi.automotive_match_status, "Matched")
		pi.submit()
		# a domestic supply: Kyushu charges consumption tax, Aichi books the same amount as input tax
		self.assertEqual(
			(si.taxes_and_charges, pi.taxes_and_charges), ("Japan Tax 10% - HPC", "Japan Tax 10% - HAMJ")
		)
		self.assertEqual(flt(si.total_taxes_and_charges), 30_000)
		self.assertEqual(flt(pi.total_taxes_and_charges), flt(si.total_taxes_and_charges))
		result = reconciliation(KYUSHU, AICHI)
		self.assertTrue(result["balanced"], result)
		self.assertEqual(
			flt(
				frappe.db.get_value(
					"Bin", {"item_code": inverter, "warehouse": catalog.PARTS_STORE}, "actual_qty"
				)
			),
			5,
		)
		self.assertEqual(
			flt(frappe.db.get_value("Bin", {"item_code": inverter, "warehouse": h.KYUSHU_FG}, "actual_qty")),
			0,
		)

	def test_an_export_to_north_america_balances_in_both_currencies(self):
		"""J-07 the other way: Aichi ships to North America in dollars at the agreed transfer price;
		Aichi's receivable (yen at the day's rate) and North America's payable (its dollars) match
		invoice by invoice, and the stock moves once."""
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_inter_company_purchase_invoice
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		from automotive_supply_chain.intercompany.handoff import create_buyer_receipt, create_seller_order
		from automotive_supply_chain.intercompany.report.goods_in_transit.goods_in_transit import in_transit
		from automotive_supply_chain.intercompany.transfer_pricing import reconciliation

		na_stock = "NA Vehicle Inventory - HADNA"
		for currency, other, rate in (("USD", "JPY", 150), ("JPY", "USD", 1 / 150)):
			if not frappe.db.exists(
				"Currency Exchange", {"date": nowdate(), "from_currency": currency, "to_currency": other}
			):
				frappe.get_doc(
					{
						"doctype": "Currency Exchange",
						"date": nowdate(),
						"from_currency": currency,
						"to_currency": other,
						"exchange_rate": rate,
						"for_buying": 1,
						"for_selling": 1,
					}
				).insert(ignore_permissions=True)
		kit = "TST-EXPORT-KIT"
		if not frappe.db.exists("Item", kit):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": kit,
					"item_name": "Test export accessory kit",
					"item_group": "Sub Assemblies",
					"stock_uom": "Nos",
					"is_stock_item": 1,
					"valuation_rate": 100_000,
					"item_defaults": [
						{"company": AICHI, "default_warehouse": catalog.FG},
						{"company": NA, "default_warehouse": na_stock},
					],
				}
			).insert(ignore_permissions=True)
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Material Receipt",
				"stock_entry_type": "Material Receipt",
				"company": AICHI,
				"items": [{"item_code": kit, "qty": 4, "t_warehouse": catalog.FG, "basic_rate": 100_000}],
			}
		)
		receipt.insert(ignore_permissions=True)
		receipt.submit()
		h.transfer_price(AICHI, NA, kit, 900, currency="USD")
		internal_supplier = frappe.db.get_value(
			"Supplier", {"is_internal_supplier": 1, "represents_company": AICHI}, "name"
		)
		with as_user(h.BUYER):
			po = frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"supplier": internal_supplier,
					"company": NA,
					"currency": "USD",
					"buying_price_list": "Intercompany USD",
					"transaction_date": nowdate(),
					"schedule_date": add_days(nowdate(), 28),
					"items": [
						{
							"item_code": kit,
							"qty": 4,
							"rate": 900,
							"warehouse": na_stock,
							"schedule_date": add_days(nowdate(), 28),
						}
					],
				}
			).insert()
		approve(po, h.BUYER, [h.PROC_MGR])
		with as_user(h.BUYER):
			po.reload()
			po.submit()
		with as_user(h.SALES_MGR):
			so = frappe.get_doc(
				"Sales Order", create_seller_order(po.name, add_days(nowdate(), 7), catalog.FG)
			)
			so.submit()
		self.assertEqual((so.company, so.currency, flt(so.conversion_rate)), (AICHI, "USD", 150))
		self.assertEqual([(r.item_code, flt(r.qty), flt(r.rate)) for r in so.items], [(kit, 4, 900)])
		dn = make_delivery_note(so.name)
		dn.insert(ignore_permissions=True)
		dn.submit()
		self.assertEqual({r.delivery_note: r.buyer for r in in_transit(nowdate())}.get(dn.name), NA)
		pr = frappe.get_doc("Purchase Receipt", create_buyer_receipt(dn.name, na_stock))
		pr.submit()
		si = make_sales_invoice(dn.name)
		si.insert(ignore_permissions=True)
		si.submit()
		pi = make_inter_company_purchase_invoice(si.name)
		pi.insert(ignore_permissions=True)
		pi.reload()
		self.assertEqual(pi.automotive_match_status, "Matched")
		pi.submit()
		# the same dollars on both sides; Aichi carries them in yen at the day's rate
		self.assertEqual((si.currency, pi.currency), ("USD", "USD"))
		self.assertEqual(flt(pi.grand_total), flt(si.grand_total))
		self.assertEqual(flt(si.net_total), 3_600)
		self.assertAlmostEqual(flt(si.base_grand_total), flt(si.grand_total) * 150, places=2)
		self.assertAlmostEqual(flt(pi.base_grand_total), flt(pi.grand_total), places=2)
		result = reconciliation(AICHI, NA)
		self.assertTrue(result["balanced"], result)
		bins = {
			warehouse: flt(
				frappe.db.get_value("Bin", {"item_code": kit, "warehouse": warehouse}, "actual_qty")
			)
			for warehouse in (catalog.FG, na_stock)
		}
		self.assertEqual(bins, {catalog.FG: 0, na_stock: 4})


class TestDealerJourney(JourneyTestCase):
	"""J-01 order to cash for one vehicle, J-09 cancellation after allocation."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.variant = cls.cat["variants"][("Premium", "AWD")]
		h.item_price(cls.variant, "Dealer Price JP", 2_500_000)
		registration = frappe.get_doc(
			{
				"doctype": "Dealer Registration",
				"dealer_name": "Asahi Motors Nagoya",
				"dealer_code": "ASAHI-NGY",
				"company": AICHI,
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
				"country": "Japan",
				"tax_id": "JP-T5550001",
				"requested_credit_limit": 100_000_000,
				"price_list": "Dealer Price JP",
				"authorized_models": [{"vehicle_model": catalog.MODEL}],
				"contacts": [
					{
						"first_name": "Mei",
						"last_name": "Asahi",
						"email": "orders@asahi-motors.example",
						"partner_role": "Sales",
					}
				],
			}
		)
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
			order = frappe.get_doc(
				{
					"doctype": "Dealer Order",
					"dealer": self.dealer,
					"dealer_reference": reference,
					"company": AICHI,
					"requested_delivery_date": add_days(nowdate(), 30),
					"items": [
						{
							"vehicle_model": catalog.MODEL,
							"configuration": config,
							"qty": 1,
							"discount_percent": 10,
						}
					],
				}
			)
			order.insert()
			order.submit()
		self.assertEqual(order.items[0].discount_percent, 0)  # dealers cannot self-discount
		from automotive_supply_chain.dealer_operations.orders import accept

		with as_user(h.LIAISON):
			result = accept(order.name)
		self.assertFalse(result["needs_approval"])
		return frappe.db.get_value("Dealer Order", order.name, "sales_order")

	def _released_vehicle(self, for_sales_order: str | None = None) -> str:
		cat = self.cat
		for item, qty in ((cat["wheel"], 4), (cat["seat"], 2)):
			if frappe.get_cached_value("Item", item, "has_batch_no"):
				batch = catalog.make_batch(item, f"B-{frappe.generate_hash(length=8)}")
				catalog.receive(item, qty, batch_no=batch)
			else:
				catalog.receive(item, qty)
		ecu = f"TECU-{frappe.generate_hash(length=8).upper()}"
		catalog.receive(cat["ecu"], 1, serial_nos=[ecu], rate=900)
		so_item = (
			frappe.get_all("Sales Order Item", filters={"parent": for_sales_order}, pluck="name", limit=1)[0]
			if for_sales_order
			else None
		)
		wo = catalog.work_order(self.variant, 1, sales_order=for_sales_order, sales_order_item=so_item)
		se = build_entry(wo.name, 1)
		vin = frappe.get_all("Vehicle Unit", filters={"work_order": wo.name}, pluck="name")[0]
		qi = frappe.get_doc(
			{
				"doctype": "Quality Inspection",
				"inspection_type": "In Process",
				"automotive_inspection_stage": "Final",
				"reference_type": "Stock Entry",
				"reference_name": se.name,
				"item_code": self.variant,
				"item_serial_no": vin,
				"sample_size": 1,
				"inspected_by": "Administrator",
				"status": "Accepted",
				"manual_inspection": 1,
				"company": AICHI,
			}
		)
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
		vin = frappe.db.get_value(
			"Vehicle Allocation", {"sales_order": so, "status": "Allocated"}, "vehicle_unit"
		)
		self.assertTrue(vin)
		unit = frappe.get_doc("Vehicle Unit", vin)
		self.assertEqual(
			(unit.inventory_status, unit.dealer, unit.sales_order), ("Reserved", self.dealer, so)
		)
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
			booking = frappe.get_doc(
				{
					"doctype": "Transport Booking",
					"carrier": carrier,
					"company": AICHI,
					"plant": catalog.PLANT,
					"direction": "Outbound",
					"vehicle_type": "Car Carrier",
					"pickup_window_start": f"{nowdate()} 09:00:00",
					"pickup_window_end": f"{nowdate()} 12:00:00",
					"delivery_window_end": f"{add_days(nowdate(), 1)} 18:00:00",
					"destination": "Asahi Motors Nagoya",
				}
			).insert()
			manifest = frappe.get_doc(
				{
					"doctype": "Shipment Manifest",
					"company": AICHI,
					"plant": catalog.PLANT,
					"transport_booking": booking.name,
					"carrier": carrier,
					"departure": f"{nowdate()} 10:00:00",
					"seal_no": "SEAL-881",
					"items": [{"delivery_note": dn.name}],
				}
			).insert()
			manifest.submit()
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "shipment_status"), "Dispatched")
		with as_user(h.LOGISTICS):
			record_delivery(manifest.name, "Taro Yard-Manager")
		with as_user(self.portal):
			acceptance = frappe.get_doc(
				{"doctype": "Delivery Acceptance", "delivery_note": dn.name, "condition": "Accepted"}
			).insert()
			acceptance.submit()
		unit.reload()
		self.assertEqual(unit.shipment_status, "Accepted")
		self.assertTrue(unit.warranty_start_date and unit.warranty_end_date)
		si = make_sales_invoice(dn.name)
		si.insert(ignore_permissions=True)
		si.submit()
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "invoice_status"), "Invoiced")
		# a domestic dealer: 10 % consumption tax, booked as output tax
		self.assertEqual(si.taxes_and_charges, "Japan Tax 10% - HAMJ")
		self.assertEqual(flt(si.total_taxes_and_charges), round(flt(si.net_total) * 0.1))
		output_tax = frappe.db.sql(
			"""select sum(credit) - sum(debit) from `tabGL Entry` where voucher_no = %s
			and account = 'CT 10%% - HAMJ' and is_cancelled = 0""",
			si.name,
		)[0][0]
		self.assertEqual(flt(output_tax), flt(si.total_taxes_and_charges))
		pe = get_payment_entry("Sales Invoice", si.name)
		pe.reference_no, pe.reference_date = "DEALER-REMIT-1", nowdate()
		pe.insert(ignore_permissions=True)
		pe.submit()
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "invoice_status"), "Paid")
		# ledgers: revenue and receivable booked, receivable cleared
		gl = frappe.db.sql(
			"""select sum(debit) - sum(credit) from `tabGL Entry` where voucher_no in (%s, %s)
			and party = %s and is_cancelled = 0""",
			(si.name, pe.name, self.dealer),
		)[0][0]
		self.assertAlmostEqual(flt(gl), 0, places=2)

	def test_built_to_order_vehicle_serves_its_own_order(self):
		from automotive_supply_chain.dealer_operations.allocation import allocate

		older = self._order("ASAHI-PO-1003")
		newer = self._order("ASAHI-PO-1004")
		vin = self._released_vehicle(for_sales_order=newer)
		allocate(AICHI)
		# FIFO alone would hand it to the older order of the same variant
		self.assertEqual(
			frappe.db.get_value(
				"Vehicle Allocation", {"vehicle_unit": vin, "status": "Allocated"}, "sales_order"
			),
			newer,
		)
		self.assertNotEqual(
			frappe.db.get_value(
				"Vehicle Allocation", {"sales_order": older, "status": "Allocated"}, "vehicle_unit"
			),
			vin,
		)
		# a vehicle built to stock then serves the oldest waiting order
		stock_vin = self._released_vehicle()
		allocate(AICHI)
		self.assertEqual(
			frappe.db.get_value(
				"Vehicle Allocation", {"vehicle_unit": stock_vin, "status": "Allocated"}, "sales_order"
			),
			older,
		)

	def test_cancellation_after_allocation_releases_reservation(self):
		from automotive_supply_chain.dealer_operations.allocation import allocate

		so = self._order("ASAHI-PO-1002")
		self._released_vehicle()
		allocate(AICHI)
		allocation = frappe.db.get_value(
			"Vehicle Allocation", {"sales_order": so, "status": "Allocated"}, "name"
		)
		vin = frappe.db.get_value("Vehicle Allocation", allocation, "vehicle_unit")
		sre = frappe.db.get_value("Vehicle Allocation", allocation, "stock_reservation_entry")
		value_before = flt(
			frappe.db.get_value("Bin", {"item_code": self.variant, "warehouse": catalog.FG}, "stock_value")
		)
		order = frappe.db.get_value("Sales Order", so, "automotive_dealer_order")
		with as_user(self.portal):
			change = frappe.get_doc(
				{
					"doctype": "Dealer Order Change",
					"dealer_order": order,
					"change_type": "Cancel Order",
					"reason": "End customer withdrew",
				}
			).insert()
			change.submit()
		change.reload()
		self.assertEqual(change.status, "Applied")
		self.assertEqual(frappe.db.get_value("Sales Order", so, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Stock Reservation Entry", sre, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Vehicle Allocation", allocation, "status"), "Released")
		unit = frappe.get_doc("Vehicle Unit", vin)
		self.assertEqual(
			(unit.inventory_status, unit.dealer, unit.automotive_allocation), ("In Stock", None, None)
		)
		value_after = flt(
			frappe.db.get_value("Bin", {"item_code": self.variant, "warehouse": catalog.FG}, "stock_value")
		)
		self.assertAlmostEqual(value_before, value_after, places=2)
		self.assertTrue(
			frappe.db.exists(
				"Audit Event", {"event_type": "allocation.released", "reference_name": allocation}
			)
		)

	def test_a_late_delivery_is_recorded_with_an_exception(self):
		"""ERP-06.4: the yard records a delivery that arrived late: the manifest shows the exception,
		logistics gets an open Delay case, and the vehicle counts as delivered."""
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		from automotive_supply_chain.dealer_operations.allocation import allocate
		from automotive_supply_chain.logistics.operations import record_delivery

		so = self._order("ASAHI-PO-1007")
		self._released_vehicle()
		allocate(AICHI)
		vin = frappe.db.get_value(
			"Vehicle Allocation", {"sales_order": so, "status": "Allocated"}, "vehicle_unit"
		)
		dn = make_delivery_note(so)
		for item in dn.items:
			item.use_serial_batch_fields = 1
			item.serial_no = vin
		dn.insert(ignore_permissions=True)
		dn.submit()
		carrier = h.supplier("Raijin Logistics", group="Carriers")
		late = "Arrived a day late: the ferry was held by the weather"
		with as_user(h.LOGISTICS):
			booking = frappe.get_doc(
				{
					"doctype": "Transport Booking",
					"carrier": carrier,
					"company": AICHI,
					"plant": catalog.PLANT,
					"direction": "Outbound",
					"vehicle_type": "Car Carrier",
					"pickup_window_start": f"{nowdate()} 09:00:00",
					"pickup_window_end": f"{nowdate()} 12:00:00",
					"delivery_window_end": f"{add_days(nowdate(), 1)} 18:00:00",
					"destination": "Asahi Motors Nagoya",
				}
			).insert()
			manifest = frappe.get_doc(
				{
					"doctype": "Shipment Manifest",
					"company": AICHI,
					"plant": catalog.PLANT,
					"transport_booking": booking.name,
					"carrier": carrier,
					"departure": f"{nowdate()} 10:00:00",
					"items": [{"delivery_note": dn.name}],
				}
			).insert()
			manifest.submit()
			self.assertEqual(record_delivery(manifest.name, "Taro Yard-Manager", exception=late), "Exception")
		recorded = frappe.db.get_value(
			"Shipment Manifest", manifest.name, ["delivery_status", "received_by_name"], as_dict=True
		)
		self.assertEqual(
			(recorded.delivery_status, recorded.received_by_name), ("Exception", "Taro Yard-Manager")
		)
		self.assertEqual(
			set(
				frappe.get_all(
					"Shipment Manifest Item", filters={"parent": manifest.name}, pluck="line_status"
				)
			),
			{"Exception"},
		)
		cases = frappe.get_all(
			"Transport Exception",
			filters={"shipment_manifest": manifest.name},
			fields=["exception_type", "status", "carrier", "description"],
		)
		self.assertEqual([tuple(c.values()) for c in cases], [("Delay", "Open", carrier, late)])
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "shipment_status"), "Delivered")
		self.assertEqual(frappe.db.get_value("Transport Booking", booking.name, "status"), "Delivered")
		with as_user(h.LOGISTICS), self.assertRaises(frappe.ValidationError):  # recorded once
			record_delivery(manifest.name, "Taro Yard-Manager")

	def test_the_carrier_reports_its_shipment_by_message(self):
		"""shipment_status from the carrier's system: confirmation, an exception, the delivery."""
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		from automotive_supply_chain.dealer_operations.allocation import allocate
		from automotive_supply_chain.integrations import handlers

		so = self._order("ASAHI-PO-1006")
		self._released_vehicle()
		allocate(AICHI)
		vin = frappe.db.get_value(
			"Vehicle Allocation", {"sales_order": so, "status": "Allocated"}, "vehicle_unit"
		)
		dn = make_delivery_note(so)
		for item in dn.items:
			item.use_serial_batch_fields = 1
			item.serial_no = vin
		dn.insert(ignore_permissions=True)
		dn.submit()
		carrier = h.supplier("Fujin Transport", group="Carriers")
		with as_user(h.LOGISTICS):
			booking = frappe.get_doc(
				{
					"doctype": "Transport Booking",
					"carrier": carrier,
					"company": AICHI,
					"plant": catalog.PLANT,
					"direction": "Outbound",
					"vehicle_type": "Car Carrier",
					"pickup_window_start": f"{nowdate()} 09:00:00",
					"pickup_window_end": f"{nowdate()} 12:00:00",
					"delivery_window_end": f"{add_days(nowdate(), 1)} 18:00:00",
					"destination": "Asahi Motors Nagoya",
				}
			).insert()
			manifest = frappe.get_doc(
				{
					"doctype": "Shipment Manifest",
					"company": AICHI,
					"plant": catalog.PLANT,
					"transport_booking": booking.name,
					"carrier": carrier,
					"departure": f"{nowdate()} 10:00:00",
					"items": [{"delivery_note": dn.name}],
				}
			).insert()
			manifest.submit()

		def partner(code: str, party: str):
			return frappe.get_doc(
				{
					"doctype": "Integration Partner",
					"partner_code": code,
					"partner_name": code,
					"partner_type": "Carrier",
					"party_type": "Supplier",
					"party": party,
					"adapter": "Local File Drop",
					"allowed_message_types": "shipment_status",
					"service_user": h.LOGISTICS,
				}
			).insert(ignore_permissions=True)

		fujin = partner("FUJIN-EDI", carrier)
		at = f"{nowdate()}T11:30:00+09:00"
		self.assertEqual(
			handlers.shipment_status(
				None,
				fujin,
				{
					"manifest": manifest.name,
					"status": "in_transit",
					"at": at,
					"carrier_reference": "FJN-55120",
				},
			),
			("Shipment Manifest", manifest.name),
		)
		self.assertEqual(
			frappe.db.get_value("Transport Booking", booking.name, "carrier_reference"), "FJN-55120"
		)
		doctype, exception = handlers.shipment_status(
			None,
			fujin,
			{
				"manifest": manifest.name,
				"status": "exception",
				"at": at,
				"exception_type": "Damage",
				"serial_no": vin,
				"description": "Scratch on the left door",
			},
		)
		self.assertEqual(
			frappe.db.get_value(doctype, exception, ["exception_type", "status", "carrier"]),
			("Damage", "Open", carrier),
		)
		handlers.shipment_status(
			None,
			fujin,
			{
				"manifest": manifest.name,
				"status": "delivered",
				"at": f"{add_days(nowdate(), 1)}T09:15:00+09:00",
				"received_by": "Ken Asahi",
			},
		)
		delivered = frappe.db.get_value(
			"Shipment Manifest", manifest.name, ["delivery_status", "received_by_name"], as_dict=True
		)
		self.assertEqual((delivered.delivery_status, delivered.received_by_name), ("Delivered", "Ken Asahi"))
		self.assertEqual(frappe.db.get_value("Vehicle Unit", vin, "shipment_status"), "Delivered")
		# reported twice: the same answer, nothing posted again
		self.assertEqual(
			handlers.shipment_status(
				None, fujin, {"manifest": manifest.name, "status": "delivered", "at": at}
			),
			("Shipment Manifest", manifest.name),
		)
		other = partner("RAIJIN-EDI", h.supplier("Raijin Logistics", group="Carriers"))
		with self.assertRaises(handlers.InboundRejected):  # another carrier's shipment
			handlers.shipment_status(
				None, other, {"manifest": manifest.name, "status": "delivered", "at": at}
			)

	def test_inbound_order_change_applies_to_the_accepted_order(self):
		"""The dealer's system sends the change (dealer_order_change, as the partner's service user)."""
		from automotive_supply_chain.integrations import handlers

		so = self._order("ASAHI-PO-1005")
		new_date = add_days(nowdate(), 45)
		with as_user(self.portal):
			doctype, name = handlers.dealer_order_change(
				None,
				None,
				{
					"dealer_reference": "ASAHI-PO-1005",
					"change_type": "Change Delivery Date",
					"new_delivery_date": new_date,
					"reason": "The end customer moved the handover",
				},
			)
		self.assertEqual(
			(doctype, frappe.db.get_value(doctype, name, "status")), ("Dealer Order Change", "Applied")
		)
		self.assertEqual(
			str(frappe.db.get_value("Sales Order Item", {"parent": so}, "delivery_date")), str(new_date)
		)
		with as_user(self.portal), self.assertRaises(handlers.InboundRejected):
			handlers.dealer_order_change(
				None,
				None,
				{
					"dealer_reference": "ASAHI-PO-0000",
					"change_type": "Cancel Order",
					"reason": "No such order",
				},
			)


class TestDealerCreditLimit(JourneyTestCase):
	"""The credit limit approved with a dealer's registration caps what acceptance commits to:
	ERPNext checks the dealer's open orders against it when the sales order is submitted."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		h.item_price(cls.cat["variants"][("Premium", "AWD")], "Dealer Price JP", 2_500_000)
		registration = frappe.get_doc(
			{
				"doctype": "Dealer Registration",
				"dealer_name": "Kurenai Motors Sendai",
				"dealer_code": "KURENAI-SDJ",
				"company": AICHI,
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
				"country": "Japan",
				"tax_id": "JP-T5550002",
				"requested_credit_limit": 3_000_000,
				"price_list": "Dealer Price JP",
				"authorized_models": [{"vehicle_model": catalog.MODEL}],
				"contacts": [
					{
						"first_name": "Aya",
						"last_name": "Kurenai",
						"email": "orders@kurenai-motors.example",
						"partner_role": "Sales",
					}
				],
			}
		)
		registration.insert(ignore_permissions=True)
		approve(registration, h.LIAISON, [h.SALES_MGR, h.CONTROLLER])
		registration.reload()
		registration.submit()
		cls.dealer = frappe.db.get_value("Dealer Registration", registration.name, "customer")
		cls.portal = "orders@kurenai-motors.example"

	def _order(self, reference: str) -> str:
		from automotive_manufacturing.product_engineering.configuration import configure

		with as_user(self.portal):
			config = configure(catalog.MODEL, {"Trim": "Premium", "Drivetrain": "AWD"})["configuration"]
			order = frappe.get_doc(
				{
					"doctype": "Dealer Order",
					"dealer": self.dealer,
					"dealer_reference": reference,
					"company": AICHI,
					"requested_delivery_date": add_days(nowdate(), 30),
					"items": [{"vehicle_model": catalog.MODEL, "configuration": config, "qty": 1}],
				}
			)
			order.insert()
			order.submit()
		return order.name

	def test_an_order_beyond_the_credit_limit_is_not_accepted(self):
		from automotive_supply_chain.dealer_operations.orders import accept

		first = self._order("KURENAI-PO-0001")
		with as_user(h.LIAISON):
			so = accept(first)["sales_order"]
		self.assertEqual(frappe.db.get_value("Sales Order", so, "docstatus"), 1)  # 2.75M with tax, limit 3M
		second = self._order("KURENAI-PO-0002")
		frappe.db.savepoint("dealer_credit_limit")
		with as_user(h.LIAISON), self.assertRaises(frappe.ValidationError) as refused:
			accept(second)
		frappe.db.rollback(save_point="dealer_credit_limit")  # what the refused request's rollback leaves
		self.assertIn("credit limit", str(refused.exception).lower())
		self.assertEqual(frappe.db.get_value("Dealer Order", first, "status"), "Accepted")
		self.assertEqual(
			frappe.db.get_value("Dealer Order", second, "status"), "Submitted"
		)  # still to decide
		self.assertFalse(frappe.db.exists("Sales Order", {"automotive_dealer_order": second}))


class TestDealerDiscountApproval(JourneyTestCase):
	"""ERP-08.4: a dealer order's discount needs the sales manager's approval from 3 %, and the
	group controller's as well from 8 % (policy Dealer Discount Approval)."""

	def test_the_approvers_grow_with_the_discount(self):
		from automotive_core.governance.approvals import request_approval

		variant = self.cat["variants"][("Premium", "AWD")]
		dealer = "Sakura Motors Kofu"
		if not frappe.db.exists("Customer", dealer):
			frappe.get_doc(
				{
					"doctype": "Customer",
					"customer_name": dealer,
					"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
					"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
				}
			).insert(ignore_permissions=True)

		def approvers(discount: float) -> list[str] | None:
			due = add_days(nowdate(), 30)
			order = frappe.get_doc(
				{
					"doctype": "Sales Order",
					"company": AICHI,
					"customer": dealer,
					"transaction_date": nowdate(),
					"delivery_date": due,
					"items": [
						{
							"item_code": variant,
							"qty": 1,
							"price_list_rate": 2_500_000,
							"discount_percentage": discount,
							"warehouse": catalog.FG,
							"delivery_date": due,
						}
					],
				}
			).insert(ignore_permissions=True)
			self.assertEqual(flt(order.automotive_max_discount_percent), discount)
			request = request_approval("Sales Order", order.name)
			return [step["role"] for step in request["steps"]]

		self.assertEqual(approvers(5), ["Sales Manager"])
		self.assertEqual(approvers(9), ["Sales Manager", "Group Controller"])


class TestVehicleCreditNote(JourneyTestCase):
	"""A credit note against a delivered vehicle's invoice marks the vehicle Credited (it moves no
	stock: the vehicle stays delivered); cancelling the credit note makes it Invoiced again."""

	def test_credit_note_on_a_delivered_vehicle_invoice(self):
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_sales_return
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		from automotive_core.governance.cancellation import cancel_with_reason

		variant = self.cat["variants"][("Premium", "AWD")]
		vin = catalog.released_vehicle(self.cat, variant)
		dealer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "Hakuba Motors Matsumoto",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			}
		).insert(ignore_permissions=True)
		dn = frappe.get_doc(
			{
				"doctype": "Delivery Note",
				"company": AICHI,
				"customer": dealer.name,
				"items": [
					{
						"item_code": variant,
						"qty": 1,
						"rate": 2_500_000,
						"warehouse": catalog.FG,
						"use_serial_batch_fields": 1,
						"serial_no": vin,
					}
				],
			}
		)
		dn.insert(ignore_permissions=True)
		dn.submit()
		invoice = make_sales_invoice(dn.name)
		invoice.insert(ignore_permissions=True)
		invoice.submit()

		def unit() -> tuple:
			states = frappe.db.get_value(
				"Vehicle Unit", vin, ["inventory_status", "invoice_status"], as_dict=True
			)
			return states.inventory_status, states.invoice_status

		self.assertEqual(unit(), ("Delivered", "Invoiced"))
		credit = make_sales_return(invoice.name)
		credit.insert(ignore_permissions=True)
		credit.submit()
		self.assertEqual((credit.is_return, credit.return_against), (1, invoice.name))
		self.assertEqual(flt(credit.grand_total), -flt(invoice.grand_total))
		self.assertEqual(unit(), ("Delivered", "Credited"))
		# the dealer owes nothing for the vehicle any more: invoice and credit note cancel out
		receivable = frappe.db.sql(
			"""select sum(debit) - sum(credit) from `tabGL Entry` where voucher_no in (%s, %s)
			and party = %s and is_cancelled = 0""",
			(invoice.name, credit.name, dealer.name),
		)[0][0]
		self.assertAlmostEqual(flt(receivable), 0, places=2)
		cancel_with_reason("Sales Invoice", credit.name, "Credit note raised against the wrong invoice")
		self.assertEqual(unit(), ("Delivered", "Invoiced"))


class TestReceivablesAndPayablesAgeing(JourneyTestCase):
	"""ERP-09.2: ERPNext's own Accounts Receivable and Accounts Payable reports age a dealer's and
	a supplier's open invoices by due date (0-30, 31-60, 61-90, 91-120, over 120 days). The
	invoices are posted today and the reports run as at a later date, aged as at that date."""

	@staticmethod
	def aged(report: str, party_type: str, party: str, days_ahead: int) -> dict:
		"""voucher -> (ageing bucket 1..5 holding its outstanding, outstanding)."""
		execute = frappe.get_attr(f"erpnext.accounts.report.{report}.{report}.execute")
		_, rows, *_ = execute(
			{
				"company": AICHI,
				"report_date": add_days(nowdate(), days_ahead),
				"age_as_on": "Report Date",
				"ageing_based_on": "Due Date",
				"range": "30, 60, 90, 120",
				"party_type": party_type,
				"party": [party],
			}
		)
		aged = {}
		for row in rows:
			if row.get("voucher_no"):
				buckets = [i for i in range(1, 6) if flt(row.get(f"range{i}"))]
				aged[row["voucher_no"]] = (buckets, flt(row.get("outstanding")))
		return aged

	def test_open_invoices_are_aged_by_due_date(self):
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

		wheel = self.cat["wheel"]
		dealer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "Fujimi Motors Kofu",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			}
		).insert(ignore_permissions=True)
		expected = {}
		for due_in, bucket in ((0, 2), (40, 1)):  # 45 and 5 days past due on the report date
			invoice = frappe.get_doc(
				{
					"doctype": "Sales Invoice",
					"company": AICHI,
					"customer": dealer.name,
					"ignore_default_payment_terms_template": 1,
					"due_date": add_days(nowdate(), due_in),
					"items": [{"item_code": wheel, "qty": 2, "rate": 1500}],
				}
			)
			invoice.insert(ignore_permissions=True)
			invoice.submit()
			expected[invoice.name] = ([bucket], flt(invoice.outstanding_amount))
		self.assertEqual(self.aged("accounts_receivable", "Customer", dealer.name, 45), expected)

		supplier = h.supplier("Kumo Fasteners")
		with as_user(h.BUYER):
			po = frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"supplier": supplier,
					"company": AICHI,
					"transaction_date": nowdate(),
					"schedule_date": add_days(nowdate(), 5),
					"items": [
						{
							"item_code": wheel,
							"qty": 10,
							"rate": 120,
							"warehouse": catalog.PARTS_STORE,
							"schedule_date": add_days(nowdate(), 5),
						}
					],
				}
			).insert()
		approve(po, h.BUYER, [h.PROC_MGR])
		with as_user(h.BUYER):
			po.reload()
			po.submit()
		receipt = frappe.get_doc(make_purchase_receipt(po.name))
		receipt.insert(ignore_permissions=True)
		receipt.submit()
		with as_user(h.AP_CLERK):
			bill = frappe.get_doc(make_purchase_invoice(receipt.name))
			bill.update(
				{
					"bill_no": "KUMO-7001",
					"ignore_default_payment_terms_template": 1,
					"payment_terms_template": None,
					"due_date": nowdate(),
				}
			)
			bill.set("payment_schedule", [])
			bill.insert()
			bill.submit()
		# 100 days past due on the report date: the 91-120 day column
		self.assertEqual(
			self.aged("accounts_payable", "Supplier", supplier, 100),
			{bill.name: ([4], flt(bill.outstanding_amount))},
		)


class TestSourcing(JourneyTestCase):
	def test_onboarding_sourcing_and_asl_enforcement(self):
		from automotive_supply_chain.supplier_collaboration.sourcing import approved_entries

		with as_user(h.BUYER):
			onboarding = frappe.get_doc(
				{
					"doctype": "Supplier Onboarding",
					"supplier_name": "Tsukimi Castings",
					"partner_code": "TSUKIMI",
					"supplier_group": "Production Material",
					"country": "Japan",
					"tax_id": "JP-T7770001",
					"risk_rating": "Low",
					"certifications": [
						{
							"standard": "IATF 16949",
							"certificate_no": "IATF-123",
							"valid_until": add_days(nowdate(), 400),
						}
					],
					"contacts": [
						{"first_name": "Ren", "email": "ren@tsukimi.example", "partner_role": "Sales"}
					],
					"proposed_items": [{"item_code": self.cat["wheel"], "plant": catalog.PLANT}],
				}
			).insert()
		approve(onboarding, h.BUYER, [h.PROC_MGR, h.QM])
		with as_user(h.BUYER):
			onboarding.reload()
			onboarding.submit()
		supplier = frappe.db.get_value("Supplier Onboarding", onboarding.name, "supplier")
		self.assertTrue(
			frappe.db.exists("Partner User Access", {"user": "ren@tsukimi.example", "supplier": supplier})
		)
		self.assertEqual(approved_entries(self.cat["wheel"], catalog.PLANT), [])  # provisional only
		with as_user(h.BUYER):
			decision = frappe.get_doc(
				{
					"doctype": "Sourcing Decision",
					"company": AICHI,
					"plant": catalog.PLANT,
					"item_code": self.cat["wheel"],
					"effective_from": nowdate(),
					"rationale": "Best value",
					"quotes": [
						{"supplier": supplier, "unit_price": 115, "lead_time_days": 3, "quality_score": 95},
						{
							"supplier": h.supplier("Kaminari Electronics"),
							"unit_price": 120,
							"lead_time_days": 3,
							"quality_score": 90,
						},
					],
				}
			).insert()
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
			frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"supplier": h.supplier("Hoshi Metalworks"),
					"company": AICHI,
					"transaction_date": nowdate(),
					"schedule_date": add_days(nowdate(), 5),
					"automotive_plant": catalog.PLANT,
					"items": [
						{
							"item_code": self.cat["wheel"],
							"qty": 1,
							"rate": 120,
							"warehouse": catalog.PARTS_STORE,
							"schedule_date": add_days(nowdate(), 5),
						}
					],
				}
			).insert()
