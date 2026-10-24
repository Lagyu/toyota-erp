"""ERP-06 supplier consignment: zero-value supplier stock, ownership and the purchase
obligation at withdrawal, agreement limits, returns and cancellation."""

from __future__ import annotations

import json

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, nowdate

from automotive_core.governance.cancellation import cancel_with_reason
from automotive_core.integration_hub import inbound
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

	def test_supplier_count_report_becomes_a_cycle_count_for_review(self):
		"""Inbound inventory update (ERP-11): the supplier's own count of its consignment stock."""
		from erpnext.stock.doctype.batch.batch import get_batch_qty

		self._move("Receipt Into Consignment", 20)
		batches = sorted((b for b in get_batch_qty(warehouse=CONSIGNMENT, item_code=self.wheel) if flt(b["qty"]) > 0),
			key=lambda b: b["batch_no"])
		batch, expected = batches[-1]["batch_no"], flt(batches[-1]["qty"])
		service = h.portal_user("vmi@kaze-wheels.example", "Supplier", self.supplier)
		frappe.get_doc({"doctype": "Integration Partner", "partner_code": "KAZE-VMI", "partner_name": self.supplier,
			"partner_type": "Supplier", "party_type": "Supplier", "party": self.supplier, "adapter": "Local File Drop",
			"allowed_message_types": "inventory_adjustment", "service_user": service}).insert(ignore_permissions=True)

		def report(message_id, reference, lines):
			frappe.flags.automotive_pending_inbound = []
			accepted = inbound.accept("KAZE-VMI", json.dumps({"message_id": message_id,
				"message_type": "inventory_adjustment", "data": {"count_reference": reference,
				"counted_at": f"{nowdate()} 07:00:00", "lines": lines}}))
			inbound.run_pending_inline()
			return frappe.get_doc("Integration Inbound Message", accepted["inbound_id"])

		first = report("KAZE-CNT-1", "CNT-0001", [{"item_code": self.wheel, "batch_no": batch, "counted_qty": expected - 2}])
		self.assertEqual((first.status, first.result_doctype), ("Processed", "Cycle Count"), first.last_error)
		count = frappe.get_doc("Cycle Count", first.result_name)
		self.assertEqual((count.docstatus, count.source, count.supplier, count.warehouse),
			(0, "Partner Report", self.supplier, CONSIGNMENT))
		line = count.lines[0]
		self.assertEqual((flt(line.expected_qty), flt(line.counted_qty), flt(line.variance_qty)),
			(expected, expected - 2, -2))
		# the same report resent under a new message id answers with the same count
		self.assertEqual(report("KAZE-CNT-1b", "CNT-0001", [{"item_code": self.wheel, "batch_no": batch,
			"counted_qty": expected - 2}]).result_name, count.name)
		# a part outside the agreement, or a batch part without its batch, is refused
		self.assertEqual(report("KAZE-CNT-2", "CNT-0002", [{"item_code": self.cat["foam"], "counted_qty": 1}]).status,
			"Rejected")
		self.assertEqual(report("KAZE-CNT-3", "CNT-0003", [{"item_code": self.wheel, "counted_qty": 1}]).status,
			"Rejected")
		# the clerk posts the reviewed count: the consignment stock follows it, still at zero value
		with as_user(h.WAREHOUSE):
			count.reload()
			count.submit()
		self.assertEqual(flt(get_batch_qty(batch_no=batch, warehouse=CONSIGNMENT)), expected - 2)
		self.assertEqual(flt(self._bin(CONSIGNMENT).stock_value), 0)
		self.assertEqual(frappe.db.get_value("Cycle Count", count.name, "status"), "Posted")
