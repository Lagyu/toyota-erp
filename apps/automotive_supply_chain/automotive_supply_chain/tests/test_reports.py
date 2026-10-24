"""Supply-chain script reports run through Frappe's report runner, and their values (and the
supplier KPIs') on documents arranged for the test."""

from __future__ import annotations

import frappe
from frappe.desk.query_report import run
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, flt, getdate, nowdate

from automotive_core.tests.utils import AICHI, approve, as_user
from automotive_manufacturing.tests import catalog
from automotive_supply_chain.tests import helpers as h


class TestSupplyChainReports(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.cat = catalog.build()
		catalog.receive(cls.cat["wheel"], 8, rate=120)

	def test_inventory_position_shows_physical_reserved_and_value(self):
		result = run("Inventory Position", filters={"company": AICHI, "item_code": self.cat["wheel"]})
		rows = [r for r in result["result"] if r["warehouse"] == catalog.LINE_SIDE]
		self.assertTrue(rows)
		row = rows[0]
		self.assertGreaterEqual(row["actual_qty"], 8)
		self.assertEqual(row["available_qty"], row["actual_qty"] - row["reserved_stock"])
		self.assertGreater(row["stock_value"], 0)

	def test_period_reports_run(self):
		period = {"from_date": add_days(nowdate(), -89), "to_date": nowdate()}
		for name in ("Purchase Price Variance", "Supplier Performance"):
			self.assertIsInstance(run(name, filters=period)["result"], list, name)
		self.assertIsInstance(
			run("Intercompany Reconciliation", filters={"to_date": nowdate()})["result"], list
		)


ITEMS = (("TST-BRACKET", "Test Bracket", 118), ("TST-HOUSING", "Test Housing", 450))


class TestSupplierReportValues(IntegrationTestCase):
	"""ERP-05.6, ERP-09.3: Supplier Performance, Purchase Price Variance, the supplier KPIs and a
	landed-cost allocation, from orders, receipts, invoices and a payment arranged for the test."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		h.users()
		catalog.build()
		for code, name, cost in ITEMS:
			if not frappe.db.exists("Item", code):
				frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": code,
						"item_name": name,
						"item_group": "Raw Material",
						"stock_uom": "Nos",
						"is_stock_item": 1,
						"is_purchase_item": 1,
						"valuation_rate": cost,
						"automotive_standard_cost": cost,
						"item_defaults": [{"company": AICHI, "default_warehouse": catalog.PARTS_STORE}],
					}
				).insert(ignore_permissions=True)

	def _order(self, supplier: str, ordered, lines: list[tuple[str, float, float, object]]):
		"""An approved purchase order; lines: (item, qty, rate, required by)."""
		with as_user(h.BUYER):
			po = frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"supplier": supplier,
					"company": AICHI,
					"transaction_date": ordered,
					"schedule_date": max(getdate(line[3]) for line in lines),
					"items": [
						{
							"item_code": item,
							"qty": qty,
							"rate": rate,
							"warehouse": catalog.PARTS_STORE,
							"schedule_date": due,
						}
						for item, qty, rate, due in lines
					],
				}
			).insert()
		approve(po, h.BUYER, [h.PROC_MGR])
		with as_user(h.BUYER):
			po.reload()
			po.submit()
		return po

	def test_supplier_performance_price_variance_and_the_supplier_kpis(self):
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

		from automotive_core.governance.approvals import decide, request_approval
		from automotive_supply_chain.analytics import kpis

		today = getdate(nowdate())
		supplier = h.supplier(f"Value Test Supplier {frappe.generate_hash(length=6)}")
		before = {
			name: fn(AICHI, today, today)
			for name, fn in (
				("otd", kpis.supplier_otd),
				("rejected", kpis.rejected_rate),
				("p2p", kpis.p2p_cycle),
			)
		}
		# ordered ten days ago: one line due three days ago (received late today), one due in five
		po = self._order(
			supplier,
			add_days(today, -10),
			[("TST-BRACKET", 10, 120, add_days(today, -3)), ("TST-BRACKET", 10, 120, add_days(today, 5))],
		)
		pr = frappe.get_doc(make_purchase_receipt(po.name))
		_late, on_time = pr.items
		on_time.received_qty, on_time.qty, on_time.rejected_qty = 10, 8, 2
		on_time.rejected_warehouse = catalog.QUARANTINE
		pr.insert(ignore_permissions=True)
		pr.submit()
		# billed below the order on the late line: 117 instead of 120
		with as_user(h.AP_CLERK):
			pi = frappe.get_doc(make_purchase_invoice(pr.name))
			pi.items[0].rate = 117
			pi.insert()
			pi.submit()
		self.assertEqual([flt(r.qty) for r in pi.items], [10, 8])

		[row] = run(
			"Supplier Performance", filters={"from_date": today, "to_date": today, "supplier": supplier}
		)["result"]
		self.assertEqual(
			(row["receipt_lines"], row["on_time_percent"], row["received_qty"], row["rejected_qty"]),
			(2, 50.0, 20, 2),
		)
		self.assertEqual(row["reject_ppm"], 100_000.0)
		self.assertAlmostEqual(row["price_variance"], (117 - 120) * 10, places=2)
		self.assertEqual((row["open_discrepancies"], row["ack_hours"]), (0, None))

		variance = run(
			"Purchase Price Variance", filters={"from_date": today, "to_date": today, "supplier": supplier}
		)["result"]
		self.assertEqual(
			[(r["item_code"], r["stock_qty"]) for r in variance], [("TST-BRACKET", 10), ("TST-BRACKET", 8)]
		)
		self.assertEqual(
			[(round(r["ppv_vs_standard"], 2), round(r["ppv_vs_order"], 2)) for r in variance],
			[((117 - 118) * 10, (117 - 120) * 10), ((120 - 118) * 8, 0)],
		)

		# paid today: ten days from the order to the payment
		with as_user(h.AP_CLERK):
			pe = get_payment_entry("Purchase Invoice", pi.name)
			pe.mode_of_payment = "Wire Transfer"
			pe.paid_from = h.company_bank_account(AICHI)
			pe.party_bank_account = h.verified_supplier_bank_account(supplier)
			pe.reference_no = f"WIRE-{frappe.generate_hash(length=6)}"
			pe.reference_date = today
			pe.insert()
			request_approval(pe.doctype, pe.name)
		req = frappe.get_last_doc("Approval Request", filters={"reference_name": pe.name}).name
		for approver in (h.TREASURY1, h.TREASURY2):
			with as_user(approver):
				decide(req, "Approve")
		with as_user(h.AP_CLERK):
			pe.reload()
			pe.submit()

		after = {
			name: fn(AICHI, today, today)
			for name, fn in (
				("otd", kpis.supplier_otd),
				("rejected", kpis.rejected_rate),
				("p2p", kpis.p2p_cycle),
			)
		}
		delta = {
			k: (after[k].numerator - before[k].numerator, after[k].denominator - before[k].denominator)
			for k in after
		}
		self.assertEqual(delta["otd"], (1, 2))  # two receipt lines, one on time
		self.assertEqual(delta["rejected"], (2, 20))  # 2 rejected of 20 received
		self.assertEqual(delta["p2p"], (10, 1))  # one invoice paid, ten days after its order
		for name, reconcile in (
			("otd", kpis.reconcile_supplier_otd),
			("rejected", kpis.reconcile_rejected_rate),
			("p2p", kpis.reconcile_p2p),
		):
			ok, detail = reconcile(AICHI, today, today, None, after[name])
			self.assertTrue(ok, f"{name}: {detail}")

	def test_a_landed_cost_voucher_spreads_freight_over_a_receipt_by_amount(self):
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

		today = getdate(nowdate())
		supplier = h.supplier(f"Value Test Supplier {frappe.generate_hash(length=6)}")
		po = self._order(
			supplier,
			today,
			[("TST-BRACKET", 10, 120, add_days(today, 3)), ("TST-HOUSING", 4, 450, add_days(today, 3))],
		)
		pr = frappe.get_doc(make_purchase_receipt(po.name))
		pr.insert(ignore_permissions=True)
		pr.submit()
		rates = {r.item_code: flt(r.valuation_rate) for r in pr.items}

		def stock_value() -> float:
			return flt(
				frappe.db.sql(
					"""select sum(stock_value_difference) from `tabStock Ledger Entry`
					where voucher_type = 'Purchase Receipt' and voucher_no = %s and is_cancelled = 0""",
					pr.name,
				)[0][0]
			)

		value = stock_value()
		expense = frappe.get_cached_value(
			"Company", AICHI, "expenses_added_to_stock_account"
		) or frappe.db.get_value(
			"Account",
			{"company": AICHI, "account_type": "Expenses Included In Valuation", "is_group": 0},
			"name",
		)
		self.assertTrue(expense)
		with as_user(h.AP_CLERK):
			lcv = frappe.get_doc(
				{
					"doctype": "Landed Cost Voucher",
					"company": AICHI,
					"posting_date": today,
					"distribute_charges_based_on": "Amount",
					"purchase_receipts": [
						{
							"receipt_document_type": "Purchase Receipt",
							"receipt_document": pr.name,
							"supplier": supplier,
							"posting_date": pr.posting_date,
							"grand_total": pr.base_grand_total,
						}
					],
					"taxes": [{"description": "Inland freight", "expense_account": expense, "amount": 600}],
				}
			)
			lcv.insert()
			lcv.submit()
		pr.reload()
		rows = {r.item_code: r for r in pr.items}
		# 600 by amount: the brackets are 1,200 of the receipt's 3,000, the housings 1,800
		self.assertEqual(
			(
				flt(rows["TST-BRACKET"].landed_cost_voucher_amount),
				flt(rows["TST-HOUSING"].landed_cost_voucher_amount),
			),
			(240, 360),
		)
		self.assertAlmostEqual(flt(rows["TST-BRACKET"].valuation_rate) - rates["TST-BRACKET"], 24, places=2)
		self.assertAlmostEqual(flt(rows["TST-HOUSING"].valuation_rate) - rates["TST-HOUSING"], 90, places=2)
		# the stock ledger and the accounts carry it: stock value up by 600, the valuation account credited
		self.assertAlmostEqual(stock_value() - value, 600, places=2)
		credited = flt(
			frappe.db.sql(
				"""select sum(credit - debit) from `tabGL Entry` where voucher_type = 'Purchase Receipt'
				and voucher_no = %s and account = %s and is_cancelled = 0""",
				(pr.name, expense),
			)[0][0]
		)
		self.assertAlmostEqual(credited, 600, places=2)
