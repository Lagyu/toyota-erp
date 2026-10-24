"""Sales and distribution flows: dealer orders (portal / EDI) and acceptance, allocation,
deliveries in carrier loads with proof of delivery and dealer acceptance, invoicing and
collection; exports Aichi -> North America with PDI; Kyushu -> Aichi powertrain
shipments with mirrored intercompany invoices and settlement."""

from __future__ import annotations

import datetime as dt
import json
import math
from collections import defaultdict

import frappe
from frappe.utils import add_days, flt, getdate, nowdate

from automotive_core.integration_hub import inbound
from automotive_seed.generator.catalog import AICHI, KYUSHU, NA
from automotive_seed.generator.flows.common import company_bank_account, fg_for

REGION_COMPANY = {"JP": AICHI, "US": NA}
PLANT_COMPANY = {"AICHI-VA": AICHI, "NA-PDC": NA}


class Sales:
	def __init__(self, sim):
		self.sim = sim
		self.ctx = sim.ctx
		self.s = sim.state["sim"]
		self.structure = sim.state["structure"]

	def portal_user(self, dealer: str) -> str | None:
		return frappe.db.get_value("Partner User Access", {"customer": dealer, "status": "Active"}, "user")

	# -----------------------------------------------------------------------------------
	# dealer orders
	# -----------------------------------------------------------------------------------
	def dealer_orders(self, day: dt.date, region: str) -> None:
		from automotive_manufacturing.product_engineering.configuration import configure
		from automotive_supply_chain.dealer_operations.orders import accept

		ctx = self.ctx
		rng = ctx.rng(f"dealer-orders-{region}", day)
		dealers = self.sim.state["dealers"][region]
		share = (1 - float(ctx.v("na_share"))) if region == "JP" else float(ctx.v("na_share"))
		lines = max(0, round(rng.gauss(float(ctx.v("dealer_order_lines_per_day")) * share, 2)))
		market = region
		variants = [v for v, info in sorted(self.structure["variants"].items()) if info["selections"]["Market"] == market]
		liaisons = [u for u in self.sim.roster.get("Dealer Liaison", [])
			if ctx.cache.get("plant_of", {}).get(u) in ((None,) if region == "JP" else ("NA-PDC",))] or \
			self.sim.roster["Dealer Liaison"]
		orders: dict[str, list] = defaultdict(list)
		for _ in range(lines):
			orders[rng.choice(dealers)].append(rng.choice(variants))
		for n, (dealer, picks) in enumerate(sorted(orders.items())):
			user = self.portal_user(dealer)
			if not user:
				continue
			reference = f"{frappe.db.get_value('Customer', dealer, 'automotive_partner_code')}-{day.strftime('%y%m%d')}-{n}"
			if frappe.db.exists("Dealer Order", {"dealer": dealer, "dealer_reference": reference}):
				continue
			with ctx.step(13, 0):
				partner = frappe.db.get_value("Integration Partner", {"party_type": "Customer", "party": dealer}, "name")
				lines_spec = []
				for variant in picks:
					info = self.structure["variants"][variant]
					lines_spec.append({"vehicle_model": info["model"], "selections": info["selections"], "qty": 1})
				if partner:
					self.sim.procurement._edi(partner, "dealer_order", f"DO-{reference}", {"dealer_reference": reference,
						"company": REGION_COMPANY[region], "requested_delivery_date": str(add_days(day, 30)),
						"lines": lines_spec})
					order = frappe.db.get_value("Dealer Order", {"dealer": dealer, "dealer_reference": reference}, "name")
				else:
					with ctx.as_user(user):
						items = [{"vehicle_model": ln["vehicle_model"], "qty": 1, "configuration":
							configure(ln["vehicle_model"], ln["selections"])["configuration"]} for ln in lines_spec]
						doc = frappe.get_doc({"doctype": "Dealer Order", "dealer": dealer, "dealer_reference": reference,
							"company": REGION_COMPANY[region], "requested_delivery_date": add_days(day, 30),
							"items": items}).insert()
						doc.submit()
					order = doc.name
				ctx.count("Dealer Order")
				liaison = liaisons[n % len(liaisons)]
				discount = rng.random() < 0.12
				if discount:
					with ctx.as_user(liaison):
						# negotiated fleet discount, granted internally before acceptance
						for row in frappe.get_all("Dealer Order Item", filters={"parent": order}, pluck="name"):
							frappe.db.set_value("Dealer Order Item", row, "discount_percent", rng.choice((2, 3.5, 4)))
				with ctx.as_user(liaison):
					result = accept(order)
				if result["needs_approval"]:
					so = frappe.get_doc("Sales Order", result["sales_order"])
					self.sim.procurement.approve_existing(so, liaison)
					with ctx.as_user(liaison):
						so.reload()
						so.submit()
				ctx.count("Sales Order")

	# -----------------------------------------------------------------------------------
	# fulfilment: allocation -> delivery -> manifest -> POD -> acceptance -> invoice
	# -----------------------------------------------------------------------------------
	def fulfil(self, day: dt.date, plant: str) -> None:
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		from automotive_supply_chain.dealer_operations.allocation import allocate
		from automotive_supply_chain.logistics.operations import record_delivery

		ctx = self.ctx
		company = PLANT_COMPANY[plant]
		logistics = self.sim.person("Logistics Coordinator", plant)
		rng = ctx.rng(f"fulfil-{plant}", day)
		with ctx.step(14, 0):
			with ctx.as_user(self.sim.person("Dealer Liaison", plant if plant == "NA-PDC" else None)):
				allocate(company)
		# deliver allocations older than a day, grouped by sales order
		ready = frappe.db.sql("""select va.name, va.sales_order, va.sales_order_item, va.vehicle_unit, va.dealer
			from `tabVehicle Allocation` va join `tabSales Order` so on so.name = va.sales_order
			where va.status = 'Allocated' and so.company = %s and date(va.allocated_at) < %s
			order by va.allocated_at""", (company, day), as_dict=True)
		by_so: dict[str, list] = defaultdict(list)
		for row in ready:
			by_so[row.sales_order].append(row)
		notes = []
		for so, rows in sorted(by_so.items()):
			with ctx.step(14, 15):
				with ctx.as_user(logistics):
					dn = make_delivery_note(so)
					wanted = {r.sales_order_item: [] for r in rows}
					for r in rows:
						wanted[r.sales_order_item].append(r.vehicle_unit)
					dn.items = [i for i in dn.items if i.so_detail in wanted]
					for item in dn.items:
						vins = wanted[item.so_detail]
						item.qty = len(vins)
						item.use_serial_batch_fields = 1
						item.serial_no = "\n".join(vins)
						item.warehouse = frappe.db.get_value("Vehicle Unit", vins[0], "current_warehouse")
					dn.insert()
					dn.submit()
				ctx.count("Delivery Note")
				notes.append(dn.name)
		carriers = self.sim.state["carriers"]
		for i in range(0, len(notes), 2):
			load = notes[i:i + 2]
			with ctx.step(15, 0):
				with ctx.as_user(logistics):
					booking = frappe.get_doc({"doctype": "Transport Booking", "carrier": carriers[i % len(carriers)],
						"company": company, "plant": plant, "direction": "Outbound", "vehicle_type": "Car Carrier",
						"pickup_window_start": f"{day} 15:00:00", "pickup_window_end": f"{day} 18:00:00",
						"delivery_window_end": f"{add_days(day, 2)} 18:00:00", "destination": "Dealer network",
						"capacity_units": 16}).insert()
					manifest = frappe.get_doc({"doctype": "Shipment Manifest", "company": company, "plant": plant,
						"transport_booking": booking.name, "carrier": booking.carrier, "departure": f"{day} 17:00:00",
						"seal_no": f"S{rng.randint(100000, 999999)}", "items": [{"delivery_note": dn} for dn in load]}).insert()
					manifest.submit()
				ctx.count("Shipment Manifest")
				self.s.setdefault("in_delivery", {})[manifest.name] = str(add_days(day, 1))
			for dn in load:
				with ctx.step(15, 30):
					with ctx.as_user(self.sim.person("AP Clerk")):
						si = make_sales_invoice(dn)
						si.insert()
						si.submit()
					ctx.count("Sales Invoice")
		# proof of delivery for yesterday's loads, then dealer acceptance
		for manifest, due in sorted(self.s.get("in_delivery", {}).items()):
			if getdate(due) > day or frappe.db.get_value("Shipment Manifest", manifest, "company") != company:
				continue
			with ctx.step(16, 0):
				with ctx.as_user(logistics):
					record_delivery(manifest, "Dealer yard manager")
				for dn in sorted({r.delivery_note for r in frappe.get_all("Shipment Manifest Item",
						filters={"parent": manifest}, fields=["delivery_note"])}):
					dealer = frappe.db.get_value("Delivery Note", dn, "customer")
					user = self.portal_user(dealer)
					if not user:
						continue
					damaged = rng.random() < 0.02
					with ctx.as_user(user):
						acc = frappe.get_doc({"doctype": "Delivery Acceptance", "delivery_note": dn,
							"condition": "Accepted With Damage" if damaged else "Accepted",
							"damage_notes": "Scratch on rear bumper" if damaged else None}).insert()
						acc.submit()
					ctx.count("Delivery Acceptance")
			self.s["in_delivery"].pop(manifest, None)

	def collect(self, day: dt.date, region: str) -> None:
		from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

		ctx = self.ctx
		company = REGION_COMPANY[region]
		cutoff = add_days(day, -int(ctx.v("dealer_payment_days")))
		rows = frappe.db.sql("""select si.name from `tabSales Invoice` si join `tabCustomer` c on c.name = si.customer
			where si.docstatus = 1 and si.company = %s and si.outstanding_amount > 0 and si.posting_date <= %s
			and c.is_internal_customer = 0 order by si.posting_date""", (company, cutoff), pluck=True)
		for name in rows:
			with ctx.step(16, 30):
				with ctx.as_user(self.sim.person("AP Clerk")):
					pe = get_payment_entry("Sales Invoice", name)
					pe.mode_of_payment = "Wire Transfer"
					pe.paid_to = company_bank_account(company)
					pe.reference_no = f"REMIT-{name}"
					pe.reference_date = day
					pe.insert()
					pe.submit()
			ctx.count("Payment Entry")

	# -----------------------------------------------------------------------------------
	# exports to North America (weekly) and North America receiving
	# -----------------------------------------------------------------------------------
	def export(self, day: dt.date) -> None:
		"""NA replenishes its stock by an intercompany order; Aichi ships released vehicles against it."""
		from erpnext.buying.doctype.purchase_order.purchase_order import make_inter_company_sales_order
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		from automotive_supply_chain.intercompany.transfer_pricing import agreed_price

		ctx = self.ctx
		rng = ctx.rng("na-replenishment", day)
		us_variants = [v for v, info in sorted(self.structure["variants"].items()) if info["selections"]["Market"] == "US"]
		weekly = round(float(ctx.v("aichi_vehicles_per_day")) * 5 * float(ctx.v("na_share")))
		internal_supplier = frappe.db.get_value("Supplier", {"is_internal_supplier": 1, "represents_company": AICHI}, "name")
		picks = defaultdict(int)
		for _ in range(weekly):
			picks[rng.choice(us_variants)] += 1
		lines = []
		for variant, qty in sorted(picks.items()):
			price = agreed_price(AICHI, NA, variant, nowdate())
			lines.append({"item_code": variant, "qty": qty, "rate": price[0], "warehouse": fg_for("NA-PDC"),
				"schedule_date": add_days(day, 28)})
		with ctx.step(15, 0):
			buyer = self.sim.person("Buyer")
			po = self.sim.procurement.purchase_order(internal_supplier, NA, lines, "NA-PDC", buyer,
				extra={"buying_price_list": "Intercompany USD", "currency": "USD"})
			with ctx.as_user(self.sim.person("Dealer Liaison")):
				so = make_inter_company_sales_order(po)
				so.delivery_date = add_days(day, 7)
				for item in so.items:
					item.warehouse = fg_for("AICHI-VA")
					item.delivery_date = so.delivery_date
				so.flags.ignore_permissions = True
				so.insert()
				so.submit()
			ctx.count("Sales Order")
		# ship released, unallocated vehicles against open export lines (FIFO)
		customer = frappe.db.get_value("Customer", {"is_internal_customer": 1, "represents_company": NA}, "name")
		open_lines = frappe.db.sql("""select soi.name, soi.parent, soi.item_code, soi.qty - soi.delivered_qty open_qty
			from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
			where so.docstatus = 1 and so.customer = %s and so.status not in ('Closed', 'Completed')
			and soi.qty > soi.delivered_qty order by so.transaction_date, soi.idx""", customer, as_dict=True)
		by_so: dict[str, list] = defaultdict(list)
		for line in open_lines:
			vins = frappe.get_all("Vehicle Unit", filters={"variant_item": line.item_code, "quality_status": "Released",
				"inventory_status": "In Stock", "automotive_allocation": ("is", "not set"),
				"current_warehouse": fg_for("AICHI-VA")}, pluck="name", order_by="built_at asc", limit=int(line.open_qty))
			if vins:
				by_so[line.parent].append((line.name, vins))
		for so_name, rows in sorted(by_so.items()):
			with ctx.step(15, 30):
				with ctx.as_user(self.sim.person("Logistics Coordinator", "AICHI-VA")):
					dn = make_delivery_note(so_name)
					wanted = dict(rows)
					dn.items = [i for i in dn.items if i.so_detail in wanted]
					for item in dn.items:
						item.qty = len(wanted[item.so_detail])
						item.use_serial_batch_fields = 1
						item.serial_no = "\n".join(wanted[item.so_detail])
					dn.flags.ignore_permissions = True
					dn.insert()
					dn.submit()
				ctx.count("Delivery Note")
				self.s.setdefault("ic_transit", {})[dn.name] = str(add_days(day, int(ctx.v("ocean_transit_days"))))
				self.intercompany_invoice(dn.name)

	def na_receiving(self, day: dt.date) -> None:
		"""Pre-delivery inspection of vehicles received in North America (receipts happen in procurement.receive)."""
		ctx = self.ctx
		for pr in list(self.s.get("pdi", [])):
			inspector = self.sim.person("Quality Inspector", "NA-PDC")
			for row in frappe.get_all("Purchase Receipt Item", filters={"parent": pr}, fields=["item_code", "serial_no"]):
				for vin in filter(None, (row.serial_no or "").split("\n")):
					with ctx.step(10, 0):
						self.sim.quality._qi(row.item_code, "Purchase Receipt", pr, "Pre-Delivery", inspector, "Accepted",
							NA, serial=vin, remarks="PDI: transport check, accessory fit")
			self.s["pdi"].remove(pr)
		# vehicles that arrived on non-working days are received on the next NA working day
		self.sim.procurement.intercompany_receipts(day)

	# -----------------------------------------------------------------------------------
	# Kyushu -> Aichi powertrains
	# -----------------------------------------------------------------------------------
	def ship_powertrains(self, day: dt.date) -> None:
		from erpnext.selling.doctype.sales_order.sales_order import make_delivery_note

		ctx = self.ctx
		customer = frappe.db.get_value("Customer", {"is_internal_customer": 1, "represents_company": AICHI}, "name")
		fg = fg_for("KYUSHU-PT")
		for so_name in frappe.get_all("Sales Order", filters={"customer": customer, "company": KYUSHU, "docstatus": 1,
				"status": ("not in", ("Closed", "Completed"))}, pluck="name", order_by="transaction_date asc"):
			so = frappe.get_doc("Sales Order", so_name)
			lines = {}
			for item in so.items:
				open_qty = int(flt(item.qty) - flt(item.delivered_qty))
				available = frappe.get_all("Serial No", filters={"item_code": item.item_code, "warehouse": fg,
					"status": "Active"}, pluck="name", order_by="creation asc", limit=open_qty) if open_qty else []
				if available:
					lines[item.name] = available
			if not lines:
				continue
			with ctx.as_user(self.sim.person("Logistics Coordinator", "KYUSHU-PT")):
				dn = make_delivery_note(so_name)
				dn.items = [i for i in dn.items if i.so_detail in lines]
				for item in dn.items:
					item.qty = len(lines[item.so_detail])
					item.use_serial_batch_fields = 1
					item.serial_no = "\n".join(lines[item.so_detail])
				dn.flags.ignore_permissions = True
				dn.insert()
				dn.submit()
			ctx.count("Delivery Note")
			self.s.setdefault("ic_transit", {})[dn.name] = str(self.sim.production.next_working_day(day))
			self.intercompany_invoice(dn.name)

	def intercompany_invoice(self, dn: str) -> None:
		"""Seller invoices at shipment; the buyer books the mirrored invoice (matched in three-way match)."""
		from erpnext.accounts.doctype.sales_invoice.sales_invoice import make_inter_company_purchase_invoice
		from erpnext.stock.doctype.delivery_note.delivery_note import make_sales_invoice

		ctx = self.ctx
		with ctx.as_user(self.sim.person("AP Clerk")):
			si = make_sales_invoice(dn)
			si.flags.ignore_permissions = True
			si.insert()
			si.submit()
			pi = make_inter_company_purchase_invoice(si.name)
			pi.bill_no = si.name
			pi.bill_date = si.posting_date
			pi.flags.ignore_permissions = True
			pi.insert()
			pi.submit()
		ctx.count("Sales Invoice")
		ctx.count("Purchase Invoice")
