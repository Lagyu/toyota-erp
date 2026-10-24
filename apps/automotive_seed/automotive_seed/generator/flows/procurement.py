"""Procurement flows: weekly MRP runs and purchase orders (with approvals), Kanban
call-offs, a supplier simulator (acknowledgements and ASNs through the portal API or
EDI file drop), receiving with incoming inspection, supplier invoices through the
three-way match, Friday payment runs with dual approval, and expedites."""

from __future__ import annotations

import datetime as dt
import json
import math
from collections import defaultdict
from pathlib import Path

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

from automotive_core.governance import approvals
from automotive_core.integration_hub import inbound, outbox
from automotive_seed.generator.catalog import AICHI, KYUSHU, NA
from automotive_seed.generator.context import working_day
from automotive_seed.generator.flows.common import company_bank_account, store_for
from automotive_seed.generator.supply import primary_supplier


def _valuation_expense_account(company: str) -> str:
	"""The company's clearing account for costs added to stock valuation (freight, duty)."""
	return frappe.get_cached_value(
		"Company", company, "expenses_added_to_stock_account"
	) or frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Expenses Included In Valuation", "is_group": 0},
		"name",
	)


# the importer's landed costs on vehicle shipments: supplier, supplier group, service item, item name
IMPORT_SERVICES = (
	("Pacific Crossing Lines Inc.", "Carriers", "SVC-OCEAN-FREIGHT", "Ocean freight (vehicle shipment)"),
	("Harbor Customs Brokerage LLC", "Services", "SVC-IMPORT-DUTY", "Import duty and customs clearance"),
)

PLANT_OF_COMPANY = {AICHI: "AICHI-VA", KYUSHU: "KYUSHU-PT", NA: "NA-PDC"}


class MaterialShortage(Exception):
	def __init__(self, item: str, qty: float, warehouse: str):
		super().__init__(f"{item}: {qty} short in {warehouse}")
		self.item, self.qty, self.warehouse = item, qty, warehouse


class Procurement:
	def __init__(self, sim):
		self.sim = sim
		self.ctx = sim.ctx
		self.s = sim.state["sim"]
		self.structure = sim.state["structure"]
		self.s.setdefault("asn_due", {})  # asn -> arrival date
		self.s.setdefault("po_followup", {})  # po -> date when the supplier ships

	# -----------------------------------------------------------------------------------
	# approvals helper
	# -----------------------------------------------------------------------------------
	def approve(self, doc, maker: str, plant: str | None = None) -> None:
		with self.ctx.as_user(maker):
			req = approvals.request_approval(doc.doctype, doc.name)
		used = {maker}
		for step in req["steps"]:
			candidates = [u for u in self.sim.roster.get(step["role"], []) if u not in used]
			if plant:
				local = [u for u in candidates if self.ctx.cache.get("plant_of", {}).get(u) in (plant, None)]
				candidates = local or candidates
			approver = candidates[0]
			used.add(approver)
			with self.ctx.as_user(approver):
				req = approvals.decide(req["name"], "Approve")
		if req["status"] != "Approved":
			raise RuntimeError(f"approval incomplete for {doc.doctype} {doc.name}: {req}")

	def approve_existing(self, doc, maker: str) -> None:
		"""Decide an approval request that was already opened (e.g. by dealer order acceptance)."""
		from automotive_core.governance.approvals import latest_request

		req = latest_request(doc.doctype, doc.name, ("Pending",))
		if not req:
			return
		used = {maker, req.requested_by}
		for step in sorted(req.steps, key=lambda s: s.step_no):
			if step.status != "Pending":
				continue
			approver = next(u for u in self.sim.roster.get(step.approver_role, []) if u not in used)
			used.add(approver)
			with self.ctx.as_user(approver):
				approvals.decide(req.name, "Approve")

	# -----------------------------------------------------------------------------------
	# purchase orders and the supplier simulator
	# -----------------------------------------------------------------------------------
	def purchase_order(
		self,
		supplier: str,
		company: str,
		lines: list[dict],
		plant: str,
		buyer: str | None = None,
		extra: dict | None = None,
	) -> str:
		buyer = buyer or self.sim.person("Buyer")
		with self.ctx.as_user(buyer):
			po = frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"supplier": supplier,
					"company": company,
					"transaction_date": nowdate(),
					"schedule_date": min(getdate(r["schedule_date"]) for r in lines),
					"automotive_plant": plant,
					"items": lines,
					**(extra or {}),
				}
			)
			po.insert()
		self.approve(po, buyer, plant)
		with self.ctx.as_user(buyer):
			po.reload()
			po.submit()
		self.ctx.count("Purchase Order")
		return po.name

	def supplier_user(self, supplier: str) -> str | None:
		return frappe.db.get_value("Partner User Access", {"supplier": supplier, "status": "Active"}, "user")

	def edi_partner(self, supplier: str) -> str | None:
		return frappe.db.get_value(
			"Integration Partner", {"party_type": "Supplier", "party": supplier, "is_active": 1}, "name"
		)

	def ship(self, po_name: str, arrival: dt.date) -> str | None:
		"""Supplier side: acknowledge (sometimes with a revised promise) and send the ASN."""
		ctx = self.ctx
		po = frappe.get_doc("Purchase Order", po_name)
		rng = ctx.rng(f"supplier:{po_name}")
		user = self.supplier_user(po.supplier)
		if not user:
			return None
		lines = [i for i in po.items if flt(i.qty) > flt(i.received_qty)]
		if not lines:
			return None
		partner = self.edi_partner(po.supplier)
		revised = rng.random() < float(ctx.v("revised_promise_rate"))
		ack = {
			"purchase_order": po.name,
			"response": "Accepted",
			"lines": [
				{
					"po_item": i.name,
					"confirmed_qty": i.qty,
					"promised_date": str(add_days(i.schedule_date, 1) if revised else i.schedule_date),
				}
				for i in lines
			],
		}
		asn_no = f"{po.name}-A{len(frappe.get_all('Advance Shipping Notice', filters={'supplier': po.supplier})) + 1}"
		asn = {
			"supplier_asn_no": asn_no,
			"ship_date": f"{nowdate()} {ctx.clock.now.time().replace(microsecond=0)}",
			"expected_arrival": f"{arrival} 06:00:00",
			"carrier": "Supplier transport",
			"lines": [
				{
					"purchase_order": po.name,
					"po_item": i.name,
					"item_code": i.item_code,
					"qty": flt(i.qty) - flt(i.received_qty),
					"supplier_lot": f"L{getdate(nowdate()).strftime('%y%m%d')}-{rng.randint(100, 999)}",
				}
				for i in lines
			],
		}
		if partner:
			self._edi(partner, "po_acknowledgement", f"ACK-{po.name}", ack)
			self._edi(partner, "asn", f"ASN-{asn_no}", asn)
			name = frappe.db.get_value(
				"Advance Shipping Notice", {"supplier": po.supplier, "supplier_asn_no": asn_no}, "name"
			)
		else:
			with ctx.as_user(user):
				ack_doc = frappe.get_doc(
					{
						"doctype": "Supplier Order Acknowledgement",
						"purchase_order": po.name,
						"ack_type": "Accepted",
						"lines": ack["lines"],
					}
				).insert()
				ack_doc.submit()
				asn_doc = frappe.get_doc(
					{
						"doctype": "Advance Shipping Notice",
						"supplier": po.supplier,
						"supplier_asn_no": asn_no,
						"ship_date": asn["ship_date"],
						"expected_arrival": asn["expected_arrival"],
						"carrier": asn["carrier"],
						"items": asn["lines"],
					}
				).insert()
				asn_doc.submit()
			name = asn_doc.name
		ctx.count("Advance Shipping Notice")
		if name:
			self.s["asn_due"][name] = str(arrival)
		return name

	def _edi(self, partner: str, message_type: str, message_id: str, data: dict) -> None:
		"""The partner simulator drops a Hinode EDI file into our inbox; the file-drop adapter accepts it."""
		from automotive_core.integration_hub.adapters.edi import encode
		from automotive_core.integration_hub.adapters.file_drop import atomic_write, partner_dir, poll_inbox

		envelope = {
			"message_id": message_id,
			"message_type": message_type,
			"schema_version": "v1",
			"sent_at": str(self.ctx.clock.now),
			"data": data,
		}
		atomic_write(Path(partner_dir(partner)) / "inbox" / f"{message_id}.edi", encode(envelope).encode())
		frappe.flags.automotive_pending_inbound = []
		poll_inbox(partner)
		results = inbound.run_pending_inline()
		if any(r != "Processed" for r in results):
			raise RuntimeError(f"EDI {message_type} {message_id} not processed: {results}")
		self.ctx.count("Integration Inbound Message")

	# -----------------------------------------------------------------------------------
	# daily receiving
	# -----------------------------------------------------------------------------------
	def receive(self, day: dt.date) -> None:
		from automotive_supply_chain.supplier_collaboration.shipments import make_purchase_receipt

		ctx = self.ctx
		due = sorted(a for a, d in self.s["asn_due"].items() if getdate(d) <= day)
		rng = ctx.rng("incoming", day)
		for asn in due:
			with ctx.step(6, 0):
				status = frappe.db.get_value("Advance Shipping Notice", asn, "status")
				if status in ("Received", "Cancelled"):
					self.s["asn_due"].pop(asn, None)
					continue
				plant = frappe.db.get_value("Advance Shipping Notice", asn, "plant") or "AICHI-VA"
				clerk = self.sim.person("Warehouse Clerk", plant)
				with ctx.as_user(clerk):
					pr = frappe.get_doc("Purchase Receipt", make_purchase_receipt(asn))
				rejected = []
				inspections = {}
				for row in pr.items:
					if not frappe.get_cached_value(
						"Item", row.item_code, "inspection_required_before_purchase"
					):
						continue
					bad = rng.random() < float(ctx.v("incoming_reject_rate"))
					inspections[row.name] = (
						self.sim.quality.incoming(row.item_code, pr.name, row.batch_no, plant, bad),
						bad,
					)
				pr.reload()  # submitting an inspection writes its reference back to the receipt
				for row in pr.items:
					if row.name not in inspections:
						continue
					row.quality_inspection, bad = inspections[row.name]
					if bad:
						row.rejected_qty, row.qty = row.qty, 0
						row.rejected_warehouse = frappe.get_cached_value(
							"Plant", plant, "quarantine_warehouse"
						)
						rejected.append(row.item_code)
				with ctx.as_user(clerk):
					pr.save()
					pr.submit()
				ctx.count("Purchase Receipt")
				self.s["asn_due"].pop(asn, None)
				self.s.setdefault("to_invoice", {})[pr.name] = str(add_days(day, 2))
				if rejected:
					self.s.setdefault("returns", []).append(pr.name)
		self.intercompany_receipts(day)
		self.returns(day)

	def intercompany_receipts(self, day: dt.date) -> None:
		from automotive_supply_chain.intercompany.handoff import create_buyer_receipt

		ctx = self.ctx
		for dn, arrival in sorted(self.s.get("ic_transit", {}).items()):
			if getdate(arrival) > day:
				continue
			buyer_company = frappe.db.get_value("Delivery Note", dn, "customer")
			buyer_company = frappe.db.get_value("Customer", buyer_company, "represents_company")
			plant = PLANT_OF_COMPANY[buyer_company]
			if buyer_company == NA and not working_day(day, "NA-PDC"):
				continue
			clerk = self.sim.person("Warehouse Clerk", plant)
			with ctx.step(6, 15):
				with ctx.as_user(clerk):
					target = (
						store_for(plant)
						if buyer_company != NA
						else frappe.get_cached_value("Plant", plant, "finished_goods_warehouse")
					)
					pr = frappe.get_doc("Purchase Receipt", create_buyer_receipt(dn, target))
					pr.submit()
			ctx.count("Purchase Receipt")
			self.s["ic_transit"].pop(dn, None)
			self.s.setdefault("to_invoice", {})[pr.name] = str(add_days(day, 2))
			if buyer_company == NA:
				self.s.setdefault("pdi", []).append(pr.name)
				self.import_costs(day, pr.name)

	def _import_services(self) -> dict[str, str]:
		"""Service item -> supplier for the importer's landed costs (created on first use, with
		the verified bank account the payment run needs)."""
		from automotive_core.finance_controls import bank_accounts as controls

		ctx = self.ctx
		expense = _valuation_expense_account(NA)
		out = {}
		for supplier, group, item, item_name in IMPORT_SERVICES:
			if not frappe.db.exists("Item", item):
				frappe.get_doc(
					{
						"doctype": "Item",
						"item_code": item,
						"item_name": item_name,
						"item_group": "Services",
						"stock_uom": "Nos",
						"is_stock_item": 0,
						"is_purchase_item": 1,
						"is_sales_item": 0,
						"item_defaults": [{"company": NA, "expense_account": expense}],
					}
				).insert(ignore_permissions=True)
				ctx.count("Item")
			if not frappe.db.exists("Supplier", supplier):
				doc = frappe.get_doc(
					{
						"doctype": "Supplier",
						"supplier_name": supplier,
						"country": "United States",
						"supplier_group": group if frappe.db.exists("Supplier Group", group) else "Carriers",
						"default_currency": "USD",
						"allow_purchase_invoice_creation_without_purchase_order": 1,
						"allow_purchase_invoice_creation_without_purchase_receipt": 1,
					}
				)
				doc.flags.accept_similar_name = True
				doc.insert(ignore_permissions=True)
				ctx.count("Supplier")
				with ctx.as_user(self.sim.person("AP Clerk")):
					account = frappe.get_doc(
						{
							"doctype": "Bank Account",
							"account_name": f"{supplier} settlement",
							"bank": "Hinode Settlement Bank",
							"party_type": "Supplier",
							"party": supplier,
							"bank_account_no": f"{9_200_000_000 + len(out)}",
						}
					).insert()
				with ctx.as_user(self.sim.person("Treasury Approver")):
					controls.verify(account.name, "Confirmed by call-back to registered phone number")
				ctx.count("Bank Account")
			out[item] = supplier
		return out

	def import_costs(self, day: dt.date, receipt: str) -> None:
		"""Ocean freight and customs duty on a vehicle shipment into North America: a buyer orders
		each from the forwarder and the customs broker (purchase orders, approved like any other),
		AP books their invoices against the orders, then a Landed Cost Voucher adds both to the
		received vehicles' valuation."""
		from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_invoice

		ctx = self.ctx
		suppliers = self._import_services()
		pr = frappe.get_doc("Purchase Receipt", receipt)
		vehicles = sum(int(flt(row.qty)) for row in pr.items)
		charges = (
			("SVC-OCEAN-FREIGHT", vehicles, float(ctx.v("ocean_freight_per_vehicle", 1150))),
			(
				"SVC-IMPORT-DUTY",
				1,
				round(flt(pr.base_net_total) * float(ctx.v("import_duty_rate", 0.025)), 2),
			),
		)
		buyer, clerk = self.sim.person("Buyer"), self.sim.person("AP Clerk")
		invoices = []
		with ctx.step(11, 0):
			for item, qty, rate in charges:
				po = self.purchase_order(
					suppliers[item],
					NA,
					[{"item_code": item, "qty": qty, "rate": rate, "schedule_date": day}],
					"NA-PDC",
					buyer,
					extra={"currency": "USD"},
				)
				with ctx.as_user(clerk):
					pi = make_purchase_invoice(po)
					pi.bill_no, pi.bill_date = f"{item[4:]}-{pr.name}", day
					pi.insert()
					pi.submit()
				invoices.append(pi)
				ctx.count("Purchase Invoice")
			expense = _valuation_expense_account(NA)
			with ctx.as_user(clerk):
				lcv = frappe.get_doc(
					{
						"doctype": "Landed Cost Voucher",
						"company": NA,
						"posting_date": day,
						"distribute_charges_based_on": "Amount",
						"purchase_receipts": [
							{
								"receipt_document_type": "Purchase Receipt",
								"receipt_document": pr.name,
								"supplier": pr.supplier,
								"posting_date": pr.posting_date,
								"grand_total": pr.base_grand_total,
							}
						],
						"taxes": [
							{
								"description": f"{invoice.items[0].item_name} ({invoice.name})",
								"expense_account": expense,
								"amount": invoice.base_total,
							}
							for invoice in invoices
						],
						"vendor_invoices": [
							{"vendor_invoice": invoice.name, "amount": invoice.base_total}
							for invoice in invoices
						],
					}
				)
				lcv.insert()
				lcv.submit()
			ctx.count("Landed Cost Voucher")

	def returns(self, day: dt.date) -> None:
		"""Rejected incoming lots: nonconformance -> Return to Supplier disposition (approved)."""
		ctx = self.ctx
		for pr in list(self.s.get("returns", [])):
			for row in frappe.get_all(
				"Purchase Receipt Item",
				filters={"parent": pr, "rejected_qty": (">", 0)},
				fields=["item_code", "batch_no", "rejected_qty", "rejected_warehouse"],
			):
				hold = frappe.db.get_value(
					"Quality Hold",
					{"item": row.item_code, "batch_no": row.batch_no, "status": "Active"},
					"name",
				)
				if not hold:
					continue
				plant = frappe.db.get_value("Warehouse", row.rejected_warehouse, "automotive_plant")
				managers = [
					u
					for u in self.sim.roster.get("Quality Manager", [])
					if ctx.cache.get("plant_of", {}).get(u) == plant
				]
				with ctx.step(10, 0):
					with ctx.as_user(managers[0]):
						disposition = frappe.get_doc(
							{
								"doctype": "Quality Disposition",
								"quality_hold": hold,
								"nonconformance": frappe.db.get_value(
									"Non Conformance", {"automotive_quality_hold": hold}, "name"
								),
								"company": frappe.get_cached_value("Plant", plant, "company"),
								"plant": plant,
								"decision": "Return to Supplier",
								"item_code": row.item_code,
								"batch_no": row.batch_no,
								"qty": row.rejected_qty,
								"from_warehouse": row.rejected_warehouse,
								"supplier": frappe.db.get_value("Purchase Receipt", pr, "supplier"),
								"justification": "Incoming inspection failed; lot returned for replacement",
							}
						).insert()
					self.approve(disposition, managers[0], plant)
					with ctx.as_user(managers[0]):
						disposition.reload()
						disposition.submit()
				ctx.count("Quality Disposition")
			self.s["returns"].remove(pr)

	# -----------------------------------------------------------------------------------
	# weekly MRP and purchase orders; intercompany powertrain orders
	# -----------------------------------------------------------------------------------
	def parameter_set(self, plant: str) -> str:
		name = frappe.db.get_value(
			"Planning Parameter Set", {"plant": plant, "docstatus": 1}, "name", order_by="version desc"
		)
		if name:
			return name
		company = frappe.get_cached_value("Plant", plant, "company")
		director = self.sim.person("Plant Director", plant)
		planner = self.sim.person("Production Planner", plant)
		with self.ctx.as_user(planner):
			doc = frappe.get_doc(
				{
					"doctype": "Planning Parameter Set",
					"parameter_set": f"{plant}-STD",
					"company": company,
					"plant": plant,
					"horizon_days": 35,
					"frozen_zone_days": 2,
					"lot_sizing": "Minimum Order Qty Multiples",
					"capacity_check": 1,
					"respect_holidays": 1,
					"include_forecast": 0,
					"include_kanban_items": 0,
					"change_note": "Initial production parameters",
				}
			).insert()
		self.approve(doc, planner, plant)
		with self.ctx.as_user(planner):
			doc.reload()
			doc.submit()
		_ = director
		return doc.name

	def weekly(self, day: dt.date) -> None:
		from automotive_manufacturing.production_planning import planning_run

		ctx = self.ctx
		for plant in ("AICHI-VA", "KYUSHU-PT"):
			planner = self.sim.person("Production Planner", plant)
			company = frappe.get_cached_value("Plant", plant, "company")
			with ctx.step(6, 30):
				params = self.parameter_set(plant)
				with ctx.as_user(planner):
					run = planning_run.start(company, plant, params)
				ctx.count("Planning Run")
				doc = frappe.get_doc("Planning Run", run)
				if doc.status != "Completed":  # never continue on a partial day
					raise RuntimeError(f"Planning Run {run} {doc.status}: {(doc.error or '')[-800:]}")
				purchases = 0
				for p in doc.proposals:
					is_buy = bool(
						p.proposal_type == "Purchase"
						and p.supplier
						and not frappe.db.get_value("Supplier", p.supplier, "is_internal_supplier")
					)
					p.decision = "Approved" if is_buy else "Rejected"
					purchases += int(is_buy)
				with ctx.as_user(planner):
					doc.save()
					if purchases:
						created = planning_run.release(run)
					else:
						created = {"Material Request": []}
			for mr in created.get("Material Request", []):
				self._orders_from_request(mr, plant)
		self.intercompany_powertrains(day)

	def _orders_from_request(self, mr: str, plant: str) -> None:
		ctx = self.ctx
		company = frappe.get_cached_value("Plant", plant, "company")
		by_supplier: dict[str, list[dict]] = defaultdict(list)
		for row in frappe.get_all(
			"Material Request Item",
			filters={"parent": mr},
			fields=["name", "item_code", "qty", "schedule_date", "warehouse", "uom", "ordered_qty"],
		):
			supplier = primary_supplier(row.item_code, plant)
			if not supplier or flt(row.qty) <= flt(row.ordered_qty):
				continue
			rate = flt(
				frappe.db.get_value(
					"Item Price",
					{"item_code": row.item_code, "supplier": supplier, "buying": 1},
					"price_list_rate",
				)
			) or flt(frappe.get_cached_value("Item", row.item_code, "automotive_standard_cost"))
			by_supplier[supplier].append(
				{
					"item_code": row.item_code,
					"qty": flt(row.qty) - flt(row.ordered_qty),
					"schedule_date": row.schedule_date,
					"warehouse": row.warehouse,
					"rate": rate,
					"material_request": mr,
					"material_request_item": row.name,
				}
			)
		for supplier, lines in sorted(by_supplier.items()):
			with ctx.step(6, 45):
				po = self.purchase_order(supplier, company, lines, plant)
			ship_day = max(
				add_days(min(getdate(r["schedule_date"]) for r in lines), -1), add_days(getdate(nowdate()), 1)
			)
			self.s["po_followup"][po] = str(ship_day)

	def intercompany_powertrains(self, day: dt.date) -> None:
		"""Aichi's weekly order for Kyushu powertrains (transfer-priced); Kyushu's mirrored sales order."""
		from automotive_supply_chain.intercompany.handoff import create_seller_order

		ctx = self.ctx
		need = self.sim.production.powertrain_need(add_days(day, 5))
		internal = frappe.db.get_value(
			"Supplier", {"is_internal_supplier": 1, "represents_company": KYUSHU}, "name"
		)
		open_qty = defaultdict(float)
		for row in frappe.db.sql(
			"""select poi.item_code, sum(poi.qty - poi.received_qty) q from `tabPurchase Order Item` poi
				join `tabPurchase Order` po on po.name = poi.parent where po.docstatus = 1 and po.supplier = %s
				and po.status not in ('Closed', 'Completed') group by poi.item_code""",
			internal,
			as_dict=True,
		):
			open_qty[row.item_code] = flt(row.q)
		from automotive_supply_chain.intercompany.transfer_pricing import agreed_price

		lines = []
		for code, qty in sorted(need.items()):
			order = math.ceil(max(qty * 1.5 - open_qty[code], 0))
			if order <= 0:
				continue
			price = agreed_price(KYUSHU, AICHI, code, nowdate())
			lines.append(
				{
					"item_code": code,
					"qty": order,
					"rate": price[0],
					"warehouse": store_for("AICHI-VA"),
					"schedule_date": add_days(day, 2),
				}
			)
		if not lines:
			return
		with ctx.step(6, 50):
			po = self.purchase_order(
				internal, AICHI, lines, "AICHI-VA", extra={"buying_price_list": "Intercompany JPY"}
			)
			with ctx.as_user(self.sim.person("Sales Manager")):  # group sales desk confirms Kyushu's order
				so = frappe.get_doc(
					"Sales Order",
					create_seller_order(
						po,
						add_days(day, 2),
						frappe.get_cached_value("Plant", "KYUSHU-PT", "finished_goods_warehouse"),
					),
				)
				so.submit()
		ctx.count("Sales Order")

	# -----------------------------------------------------------------------------------
	def replenish_forward(self, day: dt.date) -> None:
		"""After the morning receipts: the replenishment run raises transfer requests for parts
		at their reorder level (the scheduled job, as the system), and each plant's warehouse
		clerk picks them from reserve storage before the first shift draws on the store."""
		from automotive_supply_chain.logistics.storage import (
			open_transfer_requests,
			pick_from_reserve,
			raise_transfer_requests,
		)

		ctx = self.ctx
		for plant in ("AICHI-VA", "KYUSHU-PT"):
			with ctx.step(6, 30):
				if raise_transfer_requests(plant):
					ctx.count("Material Request")
			clerk = self.sim.person("Warehouse Clerk", plant)
			for request in open_transfer_requests(plant):
				with ctx.step(6, 45), ctx.as_user(clerk):
					if pick_from_reserve(request, plant):
						ctx.count("Pick List")
						ctx.count("Stock Entry")

	# -----------------------------------------------------------------------------------
	def calloffs(self, day: dt.date) -> None:
		from automotive_supply_chain.supplier_collaboration.schedules import release_kanban_calloffs

		ctx = self.ctx
		with ctx.step(12, 0):
			for po in release_kanban_calloffs():
				ctx.count("Purchase Order")
				self.ship(po, self.sim.production.next_working_day(day))
		for po, ship_day in sorted(self.s["po_followup"].items()):
			if getdate(ship_day) > day:
				continue
			with ctx.step(12, 30):
				schedule = frappe.db.sql(
					"select min(schedule_date) from `tabPurchase Order Item` where parent=%s", po
				)[0][0]
				self.ship(po, max(getdate(schedule), self.sim.production.next_working_day(day)))
			self.s["po_followup"].pop(po, None)

	def expedite(self, item: str, qty: float, warehouse: str, plant: str) -> None:
		"""Emergency supply: premium-freight purchase order received the same day."""
		from automotive_supply_chain.supplier_collaboration.shipments import make_purchase_receipt

		ctx = self.ctx
		supplier = primary_supplier(item, plant)
		if not supplier:
			# group-supplied parts (Kyushu powertrains) cannot be bought in: the unit waits
			raise MaterialShortage(item, qty, warehouse)
		company = frappe.get_cached_value("Plant", plant, "company")
		qty = (
			math.ceil(qty)
			if frappe.get_cached_value("Item", item, "stock_uom") == "Nos"
			else round(qty + 0.5, 1)
		)
		qty = max(
			qty, flt(frappe.get_cached_value("Item", item, "min_order_qty"))
		)  # suppliers ship whole MOQs
		rate = flt(frappe.get_cached_value("Item", item, "automotive_standard_cost")) * 1.08
		with ctx.frozen(ctx.clock.tick(60)):
			po = self.purchase_order(
				supplier,
				company,
				[
					{
						"item_code": item,
						"qty": qty,
						"rate": rate,
						"warehouse": warehouse,
						"schedule_date": nowdate(),
					}
				],
				plant,
			)
			asn = self.ship(po, getdate(nowdate()))
			clerk = self.sim.person("Warehouse Clerk", plant)
			with ctx.as_user(clerk):
				pr = frappe.get_doc("Purchase Receipt", make_purchase_receipt(asn))
				inspections = {
					row.name: self.sim.quality.incoming(row.item_code, pr.name, row.batch_no, plant, False)
					for row in pr.items
					if frappe.get_cached_value("Item", row.item_code, "inspection_required_before_purchase")
				}
				pr.reload()  # submitting an inspection writes its reference back to the receipt
				for row in pr.items:
					if row.name in inspections:
						row.quality_inspection = inspections[row.name]
				pr.save()
				pr.submit()
		self.s["asn_due"].pop(asn, None)
		self.s.setdefault("to_invoice", {})[pr.name] = str(add_days(getdate(nowdate()), 2))
		ctx.count("Purchase Receipt")
		ctx.count("Expedite")

	# -----------------------------------------------------------------------------------
	# supplier invoices and payments
	# -----------------------------------------------------------------------------------
	def invoices(self, day: dt.date) -> None:
		from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice

		from automotive_supply_chain.procurement_controls import three_way_match

		ctx = self.ctx
		rng = ctx.rng("invoices", day)
		for pr, due in sorted(self.s.get("to_invoice", {}).items()):
			if getdate(due) > day:
				continue
			supplier = frappe.db.get_value("Purchase Receipt", pr, "supplier")
			if frappe.db.get_value("Supplier", supplier, "is_internal_supplier"):
				self.s["to_invoice"].pop(
					pr, None
				)  # intercompany invoices mirror the seller's invoice (sales flow)
				continue
			clerk = self.sim.person("AP Clerk")
			with ctx.step(16, 0):
				with ctx.as_user(clerk):
					pi = frappe.get_doc(make_purchase_invoice(pr))
					if not pi.items:
						self.s["to_invoice"].pop(pr, None)
						continue
					variance = rng.random() < float(ctx.v("price_variance_rate"))
					if variance:
						pi.items[0].rate = round(flt(pi.items[0].rate) * 1.06, 2)
					pi.bill_no = f"INV-{pr}"
					pi.bill_date = nowdate()
					pi.insert()
				ctx.count("Purchase Invoice")
				if variance:
					manager = self.sim.person("Procurement Manager")
					for exc in frappe.get_all(
						"Invoice Match Exception",
						filters={"purchase_invoice": pi.name, "status": "Open"},
						pluck="name",
					):
						if rng.random() < 0.5:
							with ctx.as_user(manager):
								three_way_match.accept_exception(
									exc, "Raw-material surcharge agreed with supplier"
								)
						else:
							with ctx.as_user(manager):
								three_way_match.reject_exception(exc, "Bill at the order price")
							with ctx.as_user(clerk):
								pi.reload()
								po_rate = frappe.db.get_value(
									"Purchase Order Item", pi.items[0].po_detail, "rate"
								)
								pi.items[0].rate = po_rate
								pi.save()
				with ctx.as_user(clerk):
					pi.reload()
					pi.submit()
			self.s["to_invoice"].pop(pr, None)

	def payment_run(self, day: dt.date) -> None:
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		ctx = self.ctx
		cutoff = add_days(day, -14)
		rows = frappe.get_all(
			"Purchase Invoice",
			filters={
				"docstatus": 1,
				"outstanding_amount": (">", 0),
				"posting_date": ("<=", cutoff),
				"is_return": 0,
			},
			fields=["name", "supplier", "company"],
			order_by="posting_date asc",
		)
		clerk = self.sim.person("AP Clerk")
		for row in rows:
			bank_account = frappe.db.get_value(
				"Bank Account",
				{
					"party_type": "Supplier",
					"party": row.supplier,
					"automotive_verification_status": "Verified",
				},
				"name",
			)
			if not bank_account:
				continue
			with ctx.step(17, 0):
				with ctx.as_user(clerk):
					pe = get_payment_entry("Purchase Invoice", row.name)
					pe.mode_of_payment = "Wire Transfer"
					pe.paid_from = company_bank_account(row.company)
					pe.party_bank_account = bank_account
					pe.reference_no = f"PAYRUN-{day.isoformat()}-{row.name}"[:140]
					pe.reference_date = day
					pe.insert()
				self.approve(pe, clerk)
				with ctx.as_user(clerk):
					pe.reload()
					pe.submit()
			ctx.count("Payment Entry")

	def deliver_outbox(self, day: dt.date) -> None:
		with self.ctx.step(23, 0):
			while outbox.dispatch_due(limit=200):
				pass
