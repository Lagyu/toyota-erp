"""Partner portals: every read and write is confined to the caller's own organisation,
writes are idempotent, and the pages admit only the right kind of partner user."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, getdate, nowdate

from automotive_core.tests.utils import AICHI, NA, approve, as_user
from automotive_manufacturing.tests import catalog
from automotive_portals.api import dealer as dealer_api
from automotive_portals.api import supplier as supplier_api
from automotive_supply_chain.tests import helpers as h


class PortalTestCase(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		h.users()
		cls.cat = catalog.build()


def _purchase_order(supplier: str, item_code: str, qty: float = 10) -> frappe._dict:
	with as_user(h.BUYER):
		po = frappe.get_doc({"doctype": "Purchase Order", "supplier": supplier, "company": AICHI,
			"transaction_date": nowdate(), "schedule_date": add_days(nowdate(), 5), "items": [{
				"item_code": item_code, "qty": qty, "rate": 120, "warehouse": catalog.PARTS_STORE,
				"schedule_date": add_days(nowdate(), 5)}]}).insert()
	approve(po, h.BUYER, [h.PROC_MGR])
	with as_user(h.BUYER):
		po.reload()
		po.submit()
	return po


class TestSupplierPortal(PortalTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.wheel = cls.cat["wheel"]
		cls.alpha = h.supplier("Portal Alpha Components")
		cls.beta = h.supplier("Portal Beta Components")
		cls.user_alpha = h.portal_user("sales@alpha-components.example", "Supplier", cls.alpha)
		cls.user_beta = h.portal_user("sales@beta-components.example", "Supplier", cls.beta)
		cls.po_alpha = _purchase_order(cls.alpha, cls.wheel)
		cls.po_beta = _purchase_order(cls.beta, cls.wheel)
		with as_user(h.BUYER):
			rfq = frappe.get_doc({"doctype": "Request for Quotation", "company": AICHI, "transaction_date": nowdate(),
				"schedule_date": add_days(nowdate(), 20), "message_for_supplier": "Annual volume 1,200 sets",
				"suppliers": [{"supplier": cls.alpha, "send_email": 0}],
				"items": [{"item_code": cls.wheel, "qty": 1200, "uom": "Nos", "conversion_factor": 1,
					"schedule_date": add_days(nowdate(), 20), "warehouse": catalog.PARTS_STORE,
					"description": "Wheel assembly"}]}).insert()
			rfq.submit()
		cls.rfq = rfq.name

	def test_portal_roles_are_least_privilege(self):
		roles = set(frappe.get_roles(self.user_alpha))
		self.assertIn("Supplier Portal User", roles)
		self.assertNotIn("Supplier", roles)  # ERPNext's generic portal role is not granted
		self.assertEqual(frappe.db.get_value("User", self.user_alpha, "user_type"), "Website User")

	def test_orders_are_scoped_to_the_supplier(self):
		with as_user(self.user_alpha):
			names = {o.name for o in supplier_api.orders()}
			self.assertIn(self.po_alpha.name, names)
			self.assertNotIn(self.po_beta.name, names)
			self.assertEqual(supplier_api.order(self.po_alpha.name)["name"], self.po_alpha.name)
			with self.assertRaises(frappe.PermissionError):
				supplier_api.order(self.po_beta.name)
			self.assertEqual(supplier_api.dashboard()["orders_awaiting_ack"], frappe.db.count("Purchase Order",
				{"supplier": self.alpha, "docstatus": 1, "automotive_ack_status": "Awaiting"}))
		with as_user(h.BUYER), self.assertRaises(frappe.PermissionError):
			supplier_api.orders()  # desk users are not portal partners

	def test_acknowledgement_and_asn(self):
		po = self.po_alpha
		promised = add_days(nowdate(), 7)
		with as_user(self.user_alpha):
			first = supplier_api.acknowledge(po.name, "Accepted", lines=[{"po_item": po.items[0].name,
				"confirmed_qty": 10, "promised_date": promised}], idempotency_key="ack-alpha-1")
			again = supplier_api.acknowledge(po.name, "Accepted", lines=[{"po_item": po.items[0].name,
				"confirmed_qty": 10, "promised_date": promised}], idempotency_key="ack-alpha-1")
		self.assertEqual(first, again)  # replayed, not duplicated
		self.assertEqual(frappe.db.count("Supplier Order Acknowledgement", {"purchase_order": po.name}), 1)
		# a later promise than the order date is a change, even with the full quantity
		self.assertEqual(first["ack_type"], "Accepted With Changes")
		self.assertEqual(getdate(frappe.db.get_value("Purchase Order Item", po.items[0].name,
			"automotive_promised_date")), getdate(promised))

		with as_user(self.user_alpha):
			[line] = [ln for ln in supplier_api.open_lines() if ln.purchase_order == po.name]
			self.assertEqual(line.open_qty, 10)
			asn = supplier_api.create_asn("ALPHA-ASN-1", nowdate(), add_days(nowdate(), 1), lines=[{
				"purchase_order": po.name, "po_item": line.po_item, "item_code": self.wheel, "qty": 6}],
				carrier="Raijin Logistics", idempotency_key="asn-alpha-1")
			self.assertEqual(asn["status"], "In Transit")
			self.assertEqual(supplier_api.asn(asn["asn"])["lines"][0]["qty"], 6)
			[line] = [ln for ln in supplier_api.open_lines() if ln.purchase_order == po.name]
			self.assertEqual(line.open_qty, 4)
		with as_user(self.user_beta):
			with self.assertRaises(frappe.PermissionError):
				supplier_api.asn(asn["asn"])
			with self.assertRaises(frappe.PermissionError):  # cannot ship against another supplier's order
				supplier_api.create_asn("BETA-ASN-1", nowdate(), add_days(nowdate(), 1), lines=[{
					"purchase_order": po.name, "po_item": line.po_item, "item_code": self.wheel, "qty": 1}],
					idempotency_key="asn-beta-1")
			self.assertNotIn(asn["asn"], {a.name for a in supplier_api.asns()})

	def test_quotation_on_invited_rfq_only(self):
		with as_user(self.user_beta):
			self.assertEqual(supplier_api.rfqs(), [])
			with self.assertRaises(frappe.PermissionError):
				supplier_api.rfq(self.rfq)
		with as_user(self.user_alpha):
			rfq = supplier_api.rfq(self.rfq)
			self.assertEqual(rfq["message"], "Annual volume 1,200 sets")
			row = rfq["items"][0]["row"]
			result = supplier_api.submit_quotation(self.rfq, lines=[{"row": row, "qty": 1200, "rate": 118.5}],
				valid_till=add_days(nowdate(), 60), lead_time_days=6, idempotency_key="quote-alpha-1")
			with self.assertRaises(frappe.ValidationError):
				supplier_api.submit_quotation(self.rfq, lines=[{"row": row, "qty": 1200, "rate": 99}],
					valid_till=add_days(nowdate(), 60), idempotency_key="quote-alpha-2")
			self.assertEqual(supplier_api.rfqs()[0]["quote_status"], "Received")
		sq = frappe.get_doc("Supplier Quotation", result["supplier_quotation"])
		self.assertEqual((sq.docstatus, sq.supplier, sq.items[0].rate), (1, self.alpha, 118.5))
		self.assertEqual(sq.items[0].request_for_quotation, self.rfq)

	def test_notices_are_private(self):
		notice = frappe.get_doc({"doctype": "Supplier Notice", "supplier": self.alpha, "subject": "Line stop at Aichi",
			"notice_type": "Schedule Change", "message": "Deliveries move by one day."}).insert(ignore_permissions=True)
		with as_user(self.user_beta):
			self.assertNotIn(notice.name, {n.name for n in supplier_api.notices()})
			with self.assertRaises(frappe.PermissionError):
				supplier_api.mark_notice_read(notice.name)
		with as_user(self.user_alpha):
			supplier_api.mark_notice_read(notice.name)
		self.assertTrue(frappe.db.get_value("Supplier Notice", notice.name, "read_at"))

	def test_writes_require_an_idempotency_key(self):
		with as_user(self.user_alpha), self.assertRaises(frappe.ValidationError):
			supplier_api.acknowledge(self.po_alpha.name, "Rejected")

	def test_page_admits_only_suppliers(self):
		from automotive_portals.www import supplier_portal

		with as_user(self.user_alpha):
			context = frappe._dict()
			supplier_portal.get_context(context)
			self.assertEqual(context.party, self.alpha)
			self.assertIn("Supplier Portal", context.translated_messages)
		with as_user(h.BUYER), self.assertRaises(frappe.PermissionError):
			supplier_portal.get_context(frappe._dict())


class TestDealerPortal(PortalTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.variant = cls.cat["variants"][("Premium", "AWD")]
		h.item_price(cls.variant, "Dealer Price JP", 2_500_000)
		cls.dealer, cls.user = cls._dealer("Portal Motors Okazaki", "PM-OKZ", "orders@pm-okazaki.example")
		cls.other, cls.other_user = cls._dealer("Portal Motors Toyohashi", "PM-TYH", "orders@pm-toyohashi.example")

	@classmethod
	def _dealer(cls, name: str, code: str, email: str) -> tuple[str, str]:
		registration = frappe.get_doc({"doctype": "Dealer Registration", "dealer_name": name, "dealer_code": code,
			"company": AICHI, "territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			"country": "Japan", "tax_id": f"JP-{code}", "requested_credit_limit": 100_000_000,
			"price_list": "Dealer Price JP", "authorized_models": [{"vehicle_model": catalog.MODEL}],
			"contacts": [{"first_name": "Aoi", "last_name": code, "email": email, "partner_role": "Sales"}]})
		registration.insert(ignore_permissions=True)
		approve(registration, h.LIAISON, [h.SALES_MGR, h.CONTROLLER])
		registration.reload()
		registration.submit()
		return frappe.db.get_value("Dealer Registration", registration.name, "customer"), email

	def _place(self, reference: str, key: str) -> dict:
		with as_user(self.user):
			return dealer_api.place_order(reference, add_days(nowdate(), 30), lines=[{"vehicle_model": catalog.MODEL,
				"selections": {"Trim": "Premium", "Drivetrain": "AWD"}, "qty": 1}], idempotency_key=key)

	def test_configuration_check_and_order(self):
		with as_user(self.user):
			self.assertIn(catalog.MODEL, {m["name"] for m in dealer_api.models()})
			bad = dealer_api.check_configuration(catalog.MODEL, {"Trim": "Base", "Drivetrain": "AWD"})
			self.assertFalse(bad["valid"])
			self.assertIn("AWD needs the Premium trim", {v["message"] for v in bad["violations"]})
			good = dealer_api.check_configuration(catalog.MODEL, {"Trim": "Premium", "Drivetrain": "AWD"})
			self.assertEqual((good["valid"], good["variant"], good["list_price"]), (True, self.variant, 2_500_000))
			self.assertTrue(good["promise_date"])
		first = self._place("PM-OKZ-1", "order-okz-1")
		self.assertEqual(self._place("PM-OKZ-1", "order-okz-1"), first)  # replay
		order = frappe.get_doc("Dealer Order", first["dealer_order"])
		self.assertEqual((order.docstatus, order.dealer, order.company, order.total), (1, self.dealer, AICHI, 2_500_000))
		with as_user(self.user):
			self.assertIn(order.name, {o.name for o in dealer_api.orders()})
			self.assertEqual(dealer_api.order(order.name)["reference"], "PM-OKZ-1")
		with as_user(self.other_user):
			self.assertNotIn(order.name, {o.name for o in dealer_api.orders()})
			with self.assertRaises(frappe.PermissionError):
				dealer_api.order(order.name)
			with self.assertRaises(frappe.PermissionError):
				dealer_api.request_change(order.name, "Cancel Order", "not mine", idempotency_key="chg-other")

	def test_dealer_cannot_order_from_another_company(self):
		from automotive_manufacturing.product_engineering.configuration import configure

		with as_user(self.user):
			config = configure(catalog.MODEL, {"Trim": "Premium", "Drivetrain": "AWD"})["configuration"]
			with self.assertRaises(frappe.ValidationError):
				frappe.get_doc({"doctype": "Dealer Order", "dealer": self.dealer, "dealer_reference": "PM-OKZ-NA",
					"company": NA, "requested_delivery_date": add_days(nowdate(), 30),
					"items": [{"vehicle_model": catalog.MODEL, "configuration": config, "qty": 1}]}).insert()

	def test_change_request_after_acceptance(self):
		from automotive_supply_chain.dealer_operations.orders import accept

		placed = self._place("PM-OKZ-2", "order-okz-2")
		with as_user(h.LIAISON):
			accept(placed["dealer_order"])
		with as_user(self.user):
			change = dealer_api.request_change(placed["dealer_order"], "Change Delivery Date", "Customer moved date",
				new_delivery_date=add_days(nowdate(), 45), idempotency_key="chg-okz-2")
		self.assertEqual(change["status"], "Applied")
		so = frappe.db.get_value("Dealer Order", placed["dealer_order"], "sales_order")
		self.assertEqual(getdate(frappe.db.get_value("Sales Order Item", {"parent": so}, "delivery_date")),
			getdate(add_days(nowdate(), 45)))

	def test_support_requests_are_private(self):
		with as_user(self.user):
			issue = dealer_api.create_support_request("Navigation update", "Map data for 2027?",
				idempotency_key="sup-okz-1")["issue"]
			self.assertIn(issue, {i.name for i in dealer_api.support_requests()})
		self.assertEqual(frappe.db.get_value("Issue", issue, "customer"), self.dealer)
		with as_user(self.other_user):
			self.assertNotIn(issue, {i.name for i in dealer_api.support_requests()})

	def test_page_admits_only_dealers(self):
		from automotive_portals.www import dealer_portal, supplier_portal

		with as_user(self.user):
			context = frappe._dict()
			dealer_portal.get_context(context)
			self.assertEqual(context.party, self.dealer)
			with self.assertRaises(frappe.PermissionError):
				supplier_portal.get_context(frappe._dict())
