"""Printable logistics and supplier documents (P-1, ERP-12.2): the shipment manifest and the
advance shipping notice, rendered with their standard print formats for documents created
through the normal flows and printed by the people who use them."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from automotive_core.tests.utils import AICHI, approve, as_user
from automotive_manufacturing.tests import catalog
from automotive_supply_chain.logistics.operations import record_delivery
from automotive_supply_chain.tests import helpers as h


def render(doctype: str, name: str, print_format: str) -> str:
	return frappe.get_print(doctype, name, print_format=print_format, as_pdf=False)


class TestPrintFormats(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		h.users()
		cls.cat = catalog.build()

	def test_shipment_manifest(self):
		variant = self.cat["variants"][("Premium", "AWD")]
		vin = catalog.released_vehicle(self.cat, variant)
		dealer = frappe.get_doc({"doctype": "Customer", "customer_name": "Asahi Motors Nagoya",
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name")}).insert(ignore_permissions=True)
		dn = frappe.get_doc({"doctype": "Delivery Note", "company": AICHI, "customer": dealer.name,
			"items": [{"item_code": variant, "qty": 1, "rate": 2_500_000, "warehouse": catalog.FG,
				"use_serial_batch_fields": 1, "serial_no": vin}]})
		dn.insert(ignore_permissions=True)
		dn.submit()
		carrier = h.supplier("Raijin Logistics", group="Carriers")
		with as_user(h.LOGISTICS):
			booking = frappe.get_doc({"doctype": "Transport Booking", "carrier": carrier, "company": AICHI,
				"plant": catalog.PLANT, "direction": "Outbound", "vehicle_type": "Car Carrier",
				"pickup_window_start": f"{nowdate()} 09:00:00", "pickup_window_end": f"{nowdate()} 12:00:00",
				"delivery_window_end": f"{add_days(nowdate(), 1)} 18:00:00", "destination": "Nagoya delivery yard",
				"carrier_reference": "RJN-CONF-4471"}).insert()
			manifest = frappe.get_doc({"doctype": "Shipment Manifest", "company": AICHI, "plant": catalog.PLANT,
				"transport_booking": booking.name, "carrier": carrier, "departure": f"{nowdate()} 10:00:00",
				"seal_no": "SEAL-881", "truck_plate": "NGY-4471", "driver_name": "Kenta Mori",
				"items": [{"delivery_note": dn.name}]}).insert()
			manifest.submit()
			html = render("Shipment Manifest", manifest.name, "Shipment Manifest")
		for expected in (AICHI, "Shipment Manifest", manifest.name, carrier, booking.name, "RJN-CONF-4471",
				manifest.get_formatted("departure"), "SEAL-881", "NGY-4471", "Kenta Mori", "Nagoya delivery yard",
				"Car Carrier", "Aichi Vehicle Assembly", dn.name, "Asahi Motors Nagoya", vin, "In Transit"):
			self.assertIn(expected, html)
		# signature lines: the driver's (named) at dispatch, the receiver's (blank until delivery)
		for label in ("Driver", "Receiver", "Signature", "Date / Time"):
			self.assertIn(label, html)
		with as_user(h.LOGISTICS):
			record_delivery(manifest.name, "Taro Yard-Manager")
			html = render("Shipment Manifest", manifest.name, "Shipment Manifest")
		manifest.reload()
		for expected in ("Taro Yard-Manager", manifest.get_formatted("delivered_at"), "Delivered"):
			self.assertIn(expected, html)

	def test_advance_shipping_notice(self):
		wheel, ecu = self.cat["wheel"], self.cat["ecu"]
		supplier = h.supplier("Kaminari Electronics")
		due = add_days(nowdate(), 5)
		with as_user(h.BUYER):
			po = frappe.get_doc({"doctype": "Purchase Order", "supplier": supplier, "company": AICHI,
				"transaction_date": nowdate(), "schedule_date": due, "items": [
					{"item_code": wheel, "qty": 8, "rate": 120, "warehouse": catalog.PARTS_STORE, "schedule_date": due},
					{"item_code": ecu, "qty": 2, "rate": 900, "warehouse": catalog.PARTS_STORE, "schedule_date": due}]})
			po.insert()
		approve(po, h.BUYER, [h.PROC_MGR])
		with as_user(h.BUYER):
			po.reload()
			po.submit()
		asn = frappe.get_doc({"doctype": "Advance Shipping Notice", "supplier": supplier,
			"supplier_asn_no": "KAM-0002", "company": AICHI, "ship_date": f"{nowdate()} 06:00:00",
			"expected_arrival": f"{add_days(nowdate(), 1)} 08:00:00", "carrier": "Raijin Logistics",
			"tracking_no": "RJN-WB-5521", "transport_mode": "Road", "packages": 3, "gross_weight_kg": 96.5,
			"items": [
				{"purchase_order": po.name, "po_item": po.items[0].name, "item_code": wheel, "qty": 8,
					"supplier_lot": "KAM-LOT-77"},
				{"purchase_order": po.name, "po_item": po.items[1].name, "item_code": ecu, "qty": 2,
					"serial_nos": "TECU-KAM-0001\nTECU-KAM-0002"}]})
		asn.insert(ignore_permissions=True)
		asn.submit()
		with as_user(h.WAREHOUSE):  # the receiving dock prints the notice
			html = render("Advance Shipping Notice", asn.name, "Advance Shipping Notice")
		for expected in (AICHI, "Advance Shipping Notice", "KAM-0002", asn.name, supplier, "Aichi Vehicle Assembly",
				asn.get_formatted("ship_date"), asn.get_formatted("expected_arrival"), "Raijin Logistics",
				"RJN-WB-5521", "Road", "In Transit", po.name, wheel, "8 Nos", ecu, "2 Nos", "KAM-LOT-77",
				"TECU-KAM-0001, TECU-KAM-0002"):
			self.assertIn(expected, html)
		self.assertRegex(html, r"Packages</th>\s*<td>3</td>")
