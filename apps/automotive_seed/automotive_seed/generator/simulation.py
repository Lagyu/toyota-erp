"""Daily simulation of the Hinode group (one database transaction per simulated day).

Order of a working day (each step under frozen time, so every posting carries the
simulated date and a strictly increasing time):

06:00  inbound arrivals (ASN -> Purchase Receipt, incoming inspection), intercompany receipts
06:30  Monday: weekly planning runs, purchase orders, intercompany orders
07:00  Kyushu: powertrain build, inspection, shipment to Aichi
07:30  Aichi: sub-assemblies for today's sequence
08:00  Aichi: vehicle build in sequence, final inspection, rework, Andon events
12:00  Kanban call-offs to suppliers, supplier acknowledgements and ASNs
13:00  dealer orders (portal / EDI) and acceptance
14:00  allocation, deliveries, manifests, proof of delivery, dealer acceptance, invoicing
15:00  exports to North America (weekly) and North America receiving / PDI
16:00  supplier invoices (three-way match), dealer receipts
17:00  Friday: supplier payment run (dual approval); month end: FX revaluation
18:00  engineering changes, cycle counts, delegations, quality closures
19:00  tomorrow's vehicle sequence (order-driven work orders + plant schedules)
"""

from __future__ import annotations

import datetime as dt

import frappe
from frappe.utils import add_days, getdate

from automotive_seed.generator.context import Clock, SeedContext, working_day


class Simulation:
	def __init__(self, ctx: SeedContext, state: dict):
		from automotive_seed.generator.flows import finance, procurement, production, quality, sales

		self.ctx = ctx
		self.state = state
		state.setdefault("sim", {})
		self.production = production.Production(self)
		self.procurement = procurement.Procurement(self)
		self.sales = sales.Sales(self)
		self.quality = quality.Quality(self)
		self.finance = finance.Finance(self)

	@property
	def roster(self) -> dict[str, list[str]]:
		return self.state["roster"]

	def person(self, profile: str, plant: str | None = None, index: int = 0) -> str:
		users = self.roster.get(profile) or []
		if plant:
			plant_users = [u for u in users if self.ctx.cache.get("plant_of", {}).get(u) == plant] or users
			users = plant_users
		if not users:
			raise RuntimeError(f"no {profile} in the roster")
		return users[index % len(users)]

	def run_day(self, day: dt.date) -> None:
		ctx = self.ctx
		ctx.clock = Clock(day)
		from automotive_seed.generator.supply import exchange_rate

		with ctx.step(5, 0):
			exchange_rate(ctx, day)
		jp_working = working_day(day, "AICHI-VA")
		na_working = working_day(day, "NA-PDC")
		if jp_working:
			self.procurement.receive(day)
			if day.weekday() == 0:
				self.procurement.weekly(day)
			self.production.powertrains(day)
			self.production.subassemblies(day)
			self.production.vehicles(day)
			self.quality.andon(day)
			self.procurement.calloffs(day)
			self.sales.dealer_orders(day, "JP")
			self.sales.fulfil(day, "AICHI-VA")
			if day.weekday() == 2:
				self.sales.export(day)
			self.procurement.invoices(day)
			self.sales.collect(day, "JP")
			if day.weekday() == int(ctx.v("supplier_payment_weekday", 4)):
				self.procurement.payment_run(day)
			self.quality.daily(day)
			self.production.engineering(day)
			self.production.plan_tomorrow(day)
		if na_working:
			self.sales.na_receiving(day)
			self.sales.dealer_orders(day, "US")
			self.sales.fulfil(day, "NA-PDC")
			self.sales.collect(day, "US")
		self.finance.daily(day)
		if jp_working:
			self.production.handovers(day)
		self.procurement.deliver_outbox(day)
