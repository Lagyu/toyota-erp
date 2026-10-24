"""ERP-06 supplier consignment: zero-value supplier stock, ownership and the purchase
obligation at withdrawal, agreement limits, returns and cancellation."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from automotive_core.governance.cancellation import cancel_with_reason
from automotive_core.tests.utils import AICHI, as_user
from automotive_manufacturing.tests import catalog
from automotive_supply_chain.tests import helpers as h

CONSIGNMENT = "Aichi Supplier Consignment - HAMJ"


class TestConsignment(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		h.users()
		cls.cat = catalog.build()
		cls.wheel = cls.cat["wheel"]
		cls.supplier = h.supplier("Kaze Wheel Consignment")
		cls.blanket = h.blanket_order(cls.supplier, cls.wheel, 120)
		with as_user(h.PROC_MGR):
			agreement = frappe.get_doc({"doctype": "Consignment Agreement", "supplier": cls.supplier, "company": AICHI,
				"plant": catalog.PLANT, "consignment_warehouse": CONSIGNMENT, "blanket_order": cls.blanket,
				"valid_from": add_days(nowdate(), -1), "valid_to": add_days(nowdate(), 90),
				"items": [{"item_code": cls.wheel, "min_qty": 8, "max_qty": 100}]}).insert()
			agreement.submit()
		cls.agreement = agreement.name

	def _move(self, movement_type, qty, target=None):
		with as_user(h.WAREHOUSE):
			doc = frappe.get_doc({"doctype": "Consignment Movement", "movement_type": movement_type,
				"agreement": self.agreement, "posting_date": nowdate(),
				"items": [{"item_code": self.wheel, "qty": qty, "target_warehouse": target}]}).insert()
			doc.submit()
		return doc

	def _bin(self, warehouse):
		return frappe.db.get_value("Bin", {"item_code": self.wheel, "warehouse": warehouse},
			["actual_qty", "stock_value"], as_dict=True) or frappe._dict(actual_qty=0, stock_value=0)

	def test_ownership_passes_at_withdrawal(self):
		self.assertEqual(frappe.db.get_value("Consignment Agreement", self.agreement, "status"), "Active")
		self.assertEqual(flt(frappe.db.get_value("Consignment Agreement Item", {"parent": self.agreement},
			"agreed_rate")), 120)
		before_store = flt(self._bin(catalog.PARTS_STORE).actual_qty)
		self._move("Receipt Into Consignment", 40)
		self.assertEqual((flt(self._bin(CONSIGNMENT).actual_qty), flt(self._bin(CONSIGNMENT).stock_value)), (40, 0))
		with self.assertRaises(frappe.ValidationError):  # above the agreed maximum
			self._move("Receipt Into Consignment", 61)

		withdrawal = self._move("Withdrawal", 10, catalog.PARTS_STORE)
		self.assertEqual(flt(self._bin(CONSIGNMENT).actual_qty), 30)
		self.assertEqual(flt(self._bin(catalog.PARTS_STORE).actual_qty), before_store + 10)
		po = frappe.get_doc("Purchase Order", withdrawal.purchase_order)
		self.assertEqual((po.docstatus, po.automotive_release_type, po.supplier), (1, "Blanket Call-Off", self.supplier))
		self.assertEqual((flt(po.items[0].qty), flt(po.items[0].rate), po.items[0].blanket_order), (10, 120, self.blanket))
		receipt_value = flt(frappe.db.sql("""select sum(stock_value_difference) from `tabStock Ledger Entry`
			where voucher_no = %s and is_cancelled = 0""", withdrawal.purchase_receipt)[0][0])
		self.assertAlmostEqual(receipt_value, 1200, places=2)  # the plant now owns 10 wheels at the agreed price
		with self.assertRaises(frappe.ValidationError):  # cannot take more than is on consignment
			self._move("Withdrawal", 31, catalog.PARTS_STORE)

		self._move("Return To Supplier", 5)
		self.assertEqual(flt(self._bin(CONSIGNMENT).actual_qty), 25)

		with as_user(h.WAREHOUSE), self.assertRaises(frappe.PermissionError):  # clerks cannot reverse ownership
			cancel_with_reason("Consignment Movement", withdrawal.name, "Withdrawn to the wrong store")
		with as_user(h.LOGISTICS):
			cancel_with_reason("Consignment Movement", withdrawal.name, "Withdrawn to the wrong store")
		for doctype, name in (("Purchase Receipt", withdrawal.purchase_receipt),
				("Purchase Order", withdrawal.purchase_order), ("Stock Entry", withdrawal.stock_entry)):
			self.assertEqual(frappe.db.get_value(doctype, name, "docstatus"), 2, doctype)
		self.assertEqual(flt(self._bin(CONSIGNMENT).actual_qty), 35)
		self.assertEqual(flt(self._bin(catalog.PARTS_STORE).actual_qty), before_store)
