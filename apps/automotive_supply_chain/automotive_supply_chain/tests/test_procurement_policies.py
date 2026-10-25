"""The reference tenant's purchase approval matrices on real purchase orders (ERP-05.3): a
commodity's own policy ahead of the general one, by the items' item group."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from automotive_core.governance.approvals import (
	ApprovalAuthorityError,
	ApprovalRequiredError,
	decide,
	find_policy,
	request_approval,
)
from automotive_core.tests.utils import AICHI, as_user
from automotive_manufacturing.tests import catalog
from automotive_supply_chain.tests import helpers as h

CASTINGS = "Castings & Forgings"


class TestCommodityApproval(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		h.users()
		cls.cat = catalog.build()
		if not frappe.db.exists("Item Group", CASTINGS):
			frappe.get_doc(
				{"doctype": "Item Group", "item_group_name": CASTINGS, "parent_item_group": "Raw Material"}
			).insert(ignore_permissions=True)
		cls.casting = "TST-CASTING"
		if not frappe.db.exists("Item", cls.casting):
			frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": cls.casting,
					"item_name": "Test cylinder head casting",
					"item_group": CASTINGS,
					"stock_uom": "Nos",
					"is_stock_item": 1,
					"is_purchase_item": 1,
					"valuation_rate": 22_000,
					"item_defaults": [{"company": AICHI, "default_warehouse": catalog.PARTS_STORE}],
				}
			).insert(ignore_permissions=True)

	def order(self, item: str, qty: int, rate: float):
		with as_user(h.BUYER):
			return frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"supplier": h.supplier("Kurogane Foundry"),
					"company": AICHI,
					"transaction_date": nowdate(),
					"schedule_date": add_days(nowdate(), 20),
					"items": [
						{
							"item_code": item,
							"qty": qty,
							"rate": rate,
							"warehouse": catalog.PARTS_STORE,
							"schedule_date": add_days(nowdate(), 20),
						}
					],
				}
			).insert()

	def test_castings_need_supplier_quality_after_procurement(self):
		po = self.order(self.casting, 10, 22_000)  # 220,000 JPY before tax: below the director's threshold
		self.assertEqual(find_policy(po).name, f"Purchase Order Approval - Castings and Forgings ({AICHI})")
		with as_user(h.BUYER):
			req = request_approval(po.doctype, po.name)
		self.assertEqual(
			[(s["step_no"], s["role"]) for s in req["steps"]],
			[(1, "Procurement Manager"), (2, "Quality Manager")],
		)
		with as_user(h.QM), self.assertRaises(ApprovalAuthorityError):
			decide(req["name"], "Approve")  # procurement reviews first
		with as_user(h.PROC_MGR):
			self.assertEqual(decide(req["name"], "Approve")["status"], "Pending")
		with as_user(h.BUYER), self.assertRaises(ApprovalRequiredError):
			po.reload()
			po.submit()
		with as_user(h.QM):
			self.assertEqual(decide(req["name"], "Approve")["status"], "Approved")
		with as_user(h.BUYER):
			po.reload()
			po.submit()
		self.assertEqual(po.docstatus, 1)

	def test_other_materials_keep_the_general_matrix(self):
		po = self.order(self.cat["wheel"], 10, 120)
		self.assertEqual(find_policy(po).name, f"Purchase Order Approval - JPY ({AICHI})")
		with as_user(h.BUYER):
			req = request_approval(po.doctype, po.name)
		self.assertEqual([s["role"] for s in req["steps"]], ["Procurement Manager"])
