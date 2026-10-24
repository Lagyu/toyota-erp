"""Printable shop-floor and traceability documents (P-1, ERP-12.2): the Kanban card and the
vehicle certificate, rendered with their standard print formats for documents created
through the normal flows and printed by the people who use them."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_months, formatdate, get_fullname, getdate, nowdate

from automotive_core.tests.utils import AICHI, as_user, make_user
from automotive_manufacturing.printing import automotive_qr_code
from automotive_manufacturing.product_engineering.configuration import configure
from automotive_manufacturing.tests import catalog
from automotive_manufacturing.traceability.vehicle_units import set_state

QM = "qm1.mfg@hinode.example"
SUPERVISOR = "supervisor.mfg@hinode.example"


def render(doctype: str, name: str, print_format: str) -> str:
	return frappe.get_print(doctype, name, print_format=print_format, as_pdf=False)


class TestPrintFormats(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(QM, ["Quality Manager", "Stock User"])
		make_user(SUPERVISOR, ["Production Supervisor", "Manufacturing User"])
		cls.cat = catalog.build()

	def test_kanban_card(self):
		wheel, foam = self.cat["wheel"], self.cat["foam"]
		supplied = frappe.get_doc(
			{
				"doctype": "Kanban Loop",
				"loop_code": "KL-PRINT-WHEEL",
				"item_code": wheel,
				"plant": catalog.PLANT,
				"production_line": catalog.LINE,
				"supply_type": "Supplier",
				"supplier": "Kaminari Electronics",
				"line_side_warehouse": catalog.LINE_SIDE,
				"container_qty": 4,
				"number_of_cards": 3,
				"lead_time_hours": 4,
			}
		).insert(ignore_permissions=True)
		internal = frappe.get_doc(
			{
				"doctype": "Kanban Loop",
				"loop_code": "KL-PRINT-FOAM",
				"item_code": foam,
				"plant": catalog.PLANT,
				"production_line": catalog.LINE,
				"supply_type": "Internal Transfer",
				"line_side_warehouse": catalog.LINE_SIDE,
				"source_warehouse": catalog.PARTS_STORE,
				"container_qty": 7.5,
				"number_of_cards": 2,
				"lead_time_hours": 2,
			}
		).insert(ignore_permissions=True)
		card = frappe.db.get_value("Kanban Card", {"kanban_loop": supplied.name, "card_no": 2}, "name")
		self.assertEqual(card, "KL-PRINT-WHEEL-02")
		with as_user(SUPERVISOR):
			html = render("Kanban Card", card, "Kanban Card")
		for expected in (
			AICHI,
			"Kanban Card",
			"KL-PRINT-WHEEL",
			card,
			wheel,
			"4 Nos",
			"Kaminari Electronics",
			catalog.LINE_SIDE,
			catalog.LINE,
			"2 / 3",
		):
			self.assertIn(expected, html)
		# the card id is scannable: the QR code of exactly this id, and the id in plain text
		self.assertIn(f'src="{automotive_qr_code(card)}"', html)
		self.assertIn('<div class="hn-card-id">KL-PRINT-WHEEL-02</div>', html)
		# an internal loop names its source warehouse instead of a supplier
		card = frappe.db.get_value("Kanban Card", {"kanban_loop": internal.name, "card_no": 1}, "name")
		with as_user(SUPERVISOR):
			html = render("Kanban Card", card, "Kanban Card")
		for expected in (
			"KL-PRINT-FOAM-01",
			"Source Warehouse",
			catalog.PARTS_STORE,
			f"{frappe.format_value(7.5, {'fieldtype': 'Float'})} Kg",
			"1 / 2",
		):
			self.assertIn(expected, html)
		self.assertNotIn("Kaminari Electronics", html)

	def test_vehicle_certificate(self):
		variant = self.cat["variants"][("Premium", "AWD")]
		config = configure(catalog.MODEL, {"Trim": "Premium", "Drivetrain": "AWD"})["configuration"]
		vin = catalog.released_vehicle(self.cat, variant, inspector=QM, configuration=config)
		unit = frappe.get_doc("Vehicle Unit", vin)
		self.assertEqual((unit.configuration, unit.quality_status), (config, "Released"))
		with as_user(QM):
			html = render("Vehicle Unit", vin, "Vehicle Certificate")
		for expected in (
			AICHI,
			"Vehicle Certificate",
			vin,
			"H1 Test (H1T)",
			variant,
			config,
			"Trim",
			"Premium",
			"Drivetrain",
			"AWD",
			formatdate(getdate(unit.built_at)),
			"Aichi Vehicle Assembly",
			unit.final_inspection,
			"Accepted",
			"Released",
			get_fullname(QM),
			"Starts on delivery acceptance",
		):
			self.assertIn(expected, html)
		# the warranty starts when the dealer accepts the delivery (supply chain), recorded on the unit
		start, end = nowdate(), add_months(nowdate(), 36)
		set_state(vin, warranty_start_date=start, warranty_end_date=end, warranty_reference=f"WTY-{vin}")
		with as_user(QM):
			html = render("Vehicle Unit", vin, "Vehicle Certificate")
		for expected in (formatdate(start), formatdate(end), f"WTY-{vin}"):
			self.assertIn(expected, html)
		self.assertNotIn("Starts on delivery acceptance", html)
