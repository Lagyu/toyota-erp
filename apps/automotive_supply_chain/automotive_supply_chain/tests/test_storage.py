"""ERP-06 storage: put-away into the forward store up to its capacity with the rest in
reserve storage, the forward store's reorder rule, and replenishment by pick list."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from automotive_core.tests.utils import AICHI, as_user
from automotive_manufacturing.tests import catalog
from automotive_supply_chain.logistics import storage
from automotive_supply_chain.tests import helpers as h

PART = "STOR-BRACKET-01"
RESERVE = "Aichi High-Bay Reserve - HAMJ"


class TestStorage(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		h.users()
		catalog.build()
		catalog._item(PART, "Raw Material", item_name="Seat bracket (storage test)", is_purchase_item=1)

	def _bin(self, warehouse: str) -> float:
		return flt(frappe.db.get_value("Bin", {"item_code": PART, "warehouse": warehouse}, "actual_qty"))

	def _entry(self, purpose: str, qty: float, putaway: bool = False):
		row = {"item_code": PART, "qty": qty, "basic_rate": 50}
		row["t_warehouse" if purpose == "Material Receipt" else "s_warehouse"] = catalog.PARTS_STORE
		entry = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": purpose,
				"stock_entry_type": purpose,
				"company": AICHI,
				"apply_putaway_rule": int(putaway),
				"items": [row],
			}
		)
		entry.insert(ignore_permissions=True)
		entry.submit()
		return entry

	def test_putaway_reorder_and_pick_from_reserve(self):
		self.assertEqual(frappe.db.get_value("Plant", catalog.PLANT, "reserve_warehouse"), RESERVE)
		self.assertTrue(storage.set_storage(PART, catalog.PLANT, capacity=10, reorder_level=4, reorder_qty=6))
		self.assertEqual(
			storage.set_storage(PART, catalog.PLANT, capacity=10, reorder_level=4, reorder_qty=6), []
		)
		rules = frappe.get_all(
			"Putaway Rule",
			filters={"item_code": PART},
			fields=["warehouse", "priority", "stock_capacity"],
			order_by="priority",
		)
		self.assertEqual([(r.warehouse, r.priority) for r in rules], [(catalog.PARTS_STORE, 1), (RESERVE, 2)])
		self.assertEqual(flt(rules[0].stock_capacity), 10)

		# put-away: the forward store up to its capacity, the rest into reserve storage
		receipt = self._entry("Material Receipt", 25, putaway=True)
		self.assertEqual(
			sorted((r.t_warehouse, flt(r.qty)) for r in receipt.items),
			sorted([(catalog.PARTS_STORE, 10), (RESERVE, 15)]),
		)
		self.assertTrue(all(r.putaway_rule for r in receipt.items))
		self.assertTrue(
			storage.putaway_applies(
				frappe._dict(
					company=AICHI, items=[frappe._dict(item_code=PART, warehouse=catalog.PARTS_STORE)]
				)
			)
		)
		self.assertFalse(
			storage.putaway_applies(
				frappe._dict(
					company=AICHI,
					items=[  # a line also goes to line side
						frappe._dict(item_code=PART, warehouse=catalog.PARTS_STORE),
						frappe._dict(item_code=PART, warehouse=catalog.LINE_SIDE),
					],
				)
			)
		)

		# above the reorder level nothing is requested; at it, one request for the reorder quantity
		self.assertIsNone(storage.raise_transfer_requests(catalog.PLANT))
		self._entry("Material Issue", 7)
		request = storage.raise_transfer_requests(catalog.PLANT)
		lines = frappe.get_all(
			"Material Request Item",
			filters={"parent": request},
			fields=["item_code", "qty", "warehouse", "from_warehouse"],
		)
		self.assertEqual(
			[(r.item_code, flt(r.qty), r.warehouse, r.from_warehouse) for r in lines],
			[(PART, 6, catalog.PARTS_STORE, RESERVE)],
		)
		self.assertIsNone(storage.raise_transfer_requests(catalog.PLANT))  # already requested
		self.assertIn(request, storage.open_transfer_requests(catalog.PLANT))

		# the clerk picks it from reserve storage
		with as_user(h.WAREHOUSE):
			pick_list, entry = storage.pick_from_reserve(request, catalog.PLANT)
		locations = frappe.get_all(
			"Pick List Item", filters={"parent": pick_list}, fields=["warehouse", "qty"]
		)
		self.assertEqual([(r.warehouse, flt(r.qty)) for r in locations], [(RESERVE, 6)])
		self.assertEqual(
			frappe.db.get_value("Stock Entry", entry, ["pick_list", "docstatus"]), (pick_list, 1)
		)
		self.assertEqual((self._bin(catalog.PARTS_STORE), self._bin(RESERVE)), (9, 9))
		self.assertEqual(frappe.db.get_value("Material Request", request, "status"), "Transferred")
		self.assertNotIn(request, storage.open_transfer_requests(catalog.PLANT))

		# a receipt fills the forward store before the clerk picks: only what fits moves (ERPNext
		# refuses a transfer above the capacity), the rest of the request stays open
		self._entry("Material Issue", 6)
		request = storage.raise_transfer_requests(catalog.PLANT)
		self._entry("Material Receipt", 5, putaway=True)
		self.assertEqual((self._bin(catalog.PARTS_STORE), self._bin(RESERVE)), (8, 9))
		self.assertEqual(storage.forward_room(catalog.PLANT, [PART]), {PART: 2})
		with as_user(h.WAREHOUSE):
			pick_list, _entry = storage.pick_from_reserve(request, catalog.PLANT)
		self.assertEqual(flt(frappe.db.get_value("Pick List Item", {"parent": pick_list}, "qty")), 2)
		self.assertEqual((self._bin(catalog.PARTS_STORE), self._bin(RESERVE)), (10, 7))
		self.assertIn(request, storage.open_transfer_requests(catalog.PLANT))
		with as_user(h.WAREHOUSE):
			self.assertIsNone(storage.pick_from_reserve(request, catalog.PLANT))  # no room at all
		self._entry("Material Issue", 5)
		with as_user(h.WAREHOUSE):
			storage.pick_from_reserve(request, catalog.PLANT)
		self.assertEqual((self._bin(catalog.PARTS_STORE), self._bin(RESERVE)), (9, 3))
		self.assertNotIn(request, storage.open_transfer_requests(catalog.PLANT))
