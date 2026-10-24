"""Production flows: order-driven vehicle sequencing, sub-assemblies, powertrains at
Kyushu with intercompany supply to Aichi, material availability, engineering changes."""

from __future__ import annotations

import datetime as dt
import math
import zlib
from collections import Counter, defaultdict

import frappe
from frappe.utils import add_days, flt, get_datetime, getdate

from automotive_core.governance import approvals
from automotive_manufacturing.mes.production import build_entry
from automotive_seed.generator.catalog import AICHI, KYUSHU, MODELS
from automotive_seed.generator.context import working_day
from automotive_seed.generator.flows.common import fg_for, line_side_for, store_for

OPEN_WO = ("Not Started", "In Process")
LINE_SHARE = {"AICHI-FA1": 0.72, "AICHI-FA2": 0.28}
MODEL_LINE = {m["code"]: m["line"] for m in MODELS}


class Production:
	def __init__(self, sim):
		self.sim = sim
		self.ctx = sim.ctx
		self.s = sim.state["sim"]
		self.structure = sim.state["structure"]

	# -----------------------------------------------------------------------------------
	# helpers
	# -----------------------------------------------------------------------------------
	def actual(self, item: str, warehouse: str) -> float:
		return flt(frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty"))

	def next_working_day(self, day: dt.date) -> dt.date:
		nxt = add_days(day, 1)
		for _ in range(14):
			if working_day(getdate(nxt), "AICHI-VA"):
				return getdate(nxt)
			nxt = add_days(nxt, 1)
		return getdate(nxt)

	def work_order(self, item: str, qty: float, company: str, plant: str, line: str | None, source: str, fg: str,
			start: dt.datetime, planner: str, sales_order: str | None = None, so_item: str | None = None,
			configuration: str | None = None) -> str:
		with self.ctx.as_user(planner):
			wo = frappe.get_doc({
				"doctype": "Work Order", "production_item": item, "qty": qty, "company": company,
				"planned_start_date": start, "source_warehouse": source, "wip_warehouse": frappe.get_cached_value(
					"Plant", plant, "wip_warehouse"), "fg_warehouse": fg, "skip_transfer": 1, "use_multi_level_bom": 0,
				"automotive_plant": plant, "automotive_production_line": line, "sales_order": sales_order,
				"sales_order_item": so_item, "automotive_configuration": configuration,
			})
			wo.insert()
			for row in wo.required_items:
				row.source_warehouse = source
			wo.save()
			wo.submit()
		self.ctx.count("Work Order")
		return wo.name

	# -----------------------------------------------------------------------------------
	# vehicle sequence for a day (order-driven, then build-to-stock fill)
	# -----------------------------------------------------------------------------------
	def backlog(self) -> list[dict]:
		"""Open Aichi sales order lines (dealers and North America) not yet covered by work orders."""
		rows = frappe.db.sql(
			"""select soi.name, soi.parent, soi.item_code, soi.qty, soi.delivered_qty, soi.automotive_configuration,
				ifnull(so.automotive_promised_date, soi.delivery_date) due, so.transaction_date,
				(select count(*) from `tabWork Order` wo where wo.sales_order_item = soi.name and wo.docstatus = 1) planned
			from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
			where so.docstatus = 1 and so.company = %s and so.status not in ('Closed', 'Completed', 'On Hold')
			order by due, so.transaction_date, so.name, soi.idx""",
			AICHI, as_dict=True)
		out = []
		for row in rows:
			if frappe.get_cached_value("Item", row.item_code, "variant_of") not in MODEL_LINE:
				continue
			open_qty = int(flt(row.qty) - flt(row.delivered_qty) - int(row.planned))
			if open_qty > 0:
				out.append({**row, "open": open_qty})
		return out

	def plan(self, day: dt.date) -> int:
		if self.s.get(f"planned:{day}"):
			return 0
		ctx = self.ctx
		rng = ctx.rng("sequence", day)
		per_day = int(ctx.v("aichi_vehicles_per_day"))
		capacity = {line: max(1, round(per_day * share)) for line, share in LINE_SHARE.items()}
		planner = self.sim.person("Production Planner", "AICHI-VA")
		backlog = self.backlog()
		slots: dict[str, list[tuple]] = defaultdict(list)
		for row in backlog:
			model = frappe.get_cached_value("Item", row["item_code"], "variant_of")
			line = MODEL_LINE[model]
			while row["open"] > 0 and len(slots[line]) < capacity[line]:
				slots[line].append((row["item_code"], row["parent"], row["name"], row["automotive_configuration"]))
				row["open"] -= 1
		variants = sorted(self.structure["variants"])
		for line, cap in capacity.items():
			line_variants = [v for v in variants if self.structure["variants"][v]["line"] == line
				and self.structure["variants"][v]["selections"]["Market"] == "JP"]
			while len(slots[line]) < cap and line_variants:
				slots[line].append((rng.choice(line_variants), None, None, None))  # build to forecast (JP stock)
		count = 0
		for line, entries in slots.items():
			takt = flt(frappe.get_cached_value("Production Line", line, "takt_time_seconds")) or 60
			start = dt.datetime.combine(day, dt.time(6, 30))
			for i, (item, so, so_item, config) in enumerate(entries):
				self.work_order(item, 1, AICHI, "AICHI-VA", line, line_side_for(line), fg_for("AICHI-VA"),
					start + dt.timedelta(seconds=takt * i), planner, so, so_item, config)
				count += 1
			if entries:
				from automotive_manufacturing.production_planning import schedule

				with ctx.as_user(planner):
					name = schedule.build(line, day)
					schedule.set_status(name, "Released")
				ctx.count("Plant Schedule")
		self.s[f"planned:{day}"] = count
		return count

	def plan_tomorrow(self, day: dt.date) -> None:
		with self.ctx.step(19, 0):
			self.plan(self.next_working_day(day))

	# -----------------------------------------------------------------------------------
	# material availability (transfers, expedites)
	# -----------------------------------------------------------------------------------
	def ensure(self, item: str, qty: float, warehouse: str, plant: str) -> None:
		have = self.actual(item, warehouse)
		short = qty - have
		if short <= 1e-9:
			return
		store = store_for(plant)
		if store != warehouse:
			available = self.actual(item, store)
			move = min(available, math.ceil(short)) if frappe.get_cached_value("Item", item, "has_serial_no") else min(
				available, short)
			if move > 0:
				self.transfer(item, move, store, warehouse, plant)
				short -= move
		if short > 1e-9:
			self.sim.procurement.expedite(item, short, warehouse, plant)

	def transfer(self, item: str, qty: float, source: str, target: str, plant: str) -> str:
		clerk = self.sim.person("Warehouse Clerk", plant)
		company = frappe.get_cached_value("Plant", plant, "company")
		with self.ctx.as_user(clerk):
			se = frappe.get_doc({"doctype": "Stock Entry", "purpose": "Material Transfer",
				"stock_entry_type": "Material Transfer", "company": company,
				"items": [{"item_code": item, "qty": qty, "s_warehouse": source, "t_warehouse": target}]})
			se.insert()
			se.submit()
		self.ctx.count("Stock Entry")
		return se.name

	def ensure_for_work_order(self, wo_name: str, units: float = 1) -> None:
		wo = frappe.get_doc("Work Order", wo_name)
		factor = units / flt(wo.qty)
		for row in wo.required_items:
			self.ensure(row.item_code, flt(row.required_qty) * factor, row.source_warehouse or wo.source_warehouse,
				wo.automotive_plant)

	def build(self, wo_name: str, units: float, operator: str) -> str:
		self.ensure_for_work_order(wo_name, units)
		self.confirm_operations(wo_name, units, operator)
		with self.ctx.as_user(operator):
			se = build_entry(wo_name, units)
		self.ctx.count("Stock Entry")
		return se.name

	def confirm_operations(self, wo_name: str, units: float, supervisor: str) -> None:
		"""The stations' confirmations before ``units`` of a routed work order are posted (ERP-04.1):
		each operation not yet confirmed for them is confirmed for the order's whole quantity, a time
		log at the standard time for an operator assigned to the line (the labour record), back to
		back on the line's clock (a station runs one job at a time, an operator works on one at a
		time), by the supervisor who posts the build."""
		from automotive_manufacturing.mes.production import record_operation

		wo = frappe.get_doc("Work Order", wo_name)
		if not wo.operations:
			return
		ctx = self.ctx
		need = flt(wo.produced_qty) + flt(units)
		line = wo.automotive_production_line
		operator = frappe.db.sql("""select e.user_id from `tabPlant Assignment` pa join `tabEmployee` e
			on e.name = pa.employee join `tabHas Role` r on r.parent = e.user_id and r.role = 'Line Operator'
			where pa.production_line = %s order by e.user_id limit 1""", line, pluck=True)
		operator = operator[0] if operator else self.sim.person("Line Operator", wo.automotive_plant)
		employee = frappe.db.get_value("Employee", {"user_id": operator}, "name")
		clocks = self.s.setdefault("line_clock", {})
		start = max(get_datetime(clocks[line]) if clocks.get(line) else ctx.clock.now, ctx.clock.now)
		for op in sorted(wo.operations, key=lambda r: r.idx):
			if flt(op.completed_qty) >= need - 1e-9:
				continue
			minutes = flt(op.time_in_mins) / flt(wo.qty) if flt(wo.qty) else flt(op.time_in_mins)
			for card in frappe.get_all("Job Card", filters={"work_order": wo.name, "operation_id": op.name,
					"docstatus": 0}, fields=["name", "for_quantity", "total_completed_qty"], order_by="creation asc, name asc"):
				qty = flt(card.for_quantity) - flt(card.total_completed_qty)
				if qty <= 1e-9:
					continue
				end = start + dt.timedelta(minutes=minutes * qty)
				with ctx.as_user(supervisor):
					record_operation(card.name, qty, employee, start, end)
				ctx.count("Job Card")
				start = end
		clocks[line] = start.isoformat()

	# -----------------------------------------------------------------------------------
	# sub-assemblies for today's vehicles
	# -----------------------------------------------------------------------------------
	def subassemblies(self, day: dt.date) -> None:
		ctx = self.ctx
		with ctx.step(7, 30):
			if not self.s.get(f"planned:{day}"):
				self.plan(day)
			needs: Counter = Counter()
			for wo in frappe.get_all("Work Order", filters={"automotive_plant": "AICHI-VA", "docstatus": 1,
					"status": ("in", OPEN_WO), "planned_start_date": ("between", (f"{day} 00:00:00", f"{day} 23:59:59")),
					"production_item": ("in", list(self.structure["variants"]))}, fields=["name", "source_warehouse"]):
				for row in frappe.get_all("Work Order Item", filters={"parent": wo.name}, fields=["item_code", "required_qty"]):
					if row.item_code in self.structure["modules"]:
						needs[(row.item_code, wo.source_warehouse)] += flt(row.required_qty)
			planner = self.sim.person("Production Planner", "AICHI-VA")
			supervisor = self.sim.person("Production Supervisor", "AICHI-VA")
			for (module, warehouse), qty in sorted(needs.items()):
				short = qty - self.actual(module, warehouse)
				if short <= 0:
					continue
				batch = math.ceil(short / 5) * 5
				info = self.structure["modules"][module]
				source = line_side_for(info["line"])
				start = ctx.clock.tick(60)
				wo = self.work_order(module, batch, AICHI, "AICHI-VA", info["line"], source, warehouse, start, planner)
				self.build(wo, batch, supervisor)

	# -----------------------------------------------------------------------------------
	# vehicles: build in schedule sequence, final inspection
	# -----------------------------------------------------------------------------------
	def vehicles(self, day: dt.date) -> None:
		from automotive_seed.generator.flows.procurement import MaterialShortage

		ctx = self.ctx
		rate = float(ctx.v("final_reject_rate"))
		rng = ctx.rng("final-inspection", day)
		schedules = frappe.get_all("Plant Schedule", filters={"schedule_date": day, "plant": "AICHI-VA",
			"status": "Released"}, pluck="name", order_by="production_line asc")
		inspector = self.sim.person("Quality Inspector", "AICHI-VA")
		for schedule in schedules:
			doc = frappe.get_doc("Plant Schedule", schedule)
			operator = self.sim.person("Production Supervisor", "AICHI-VA", zlib.crc32(doc.production_line.encode()))
			for slot in sorted(doc.slots, key=lambda s: s.sequence_no):
				if frappe.db.get_value("Work Order", slot.work_order, "status") not in OPEN_WO:
					continue
				try:
					with ctx.step(8, 0):
						se = self.build(slot.work_order, 1, operator)
				except MaterialShortage as short:
					self.s.setdefault("shortages", []).append({"day": str(day), "work_order": slot.work_order,
						"item": short.item, "qty": short.qty})
					continue
				vin = slot.vehicle_unit
				with ctx.step(8, 0):
					self.sim.quality.final_inspection(vin, se, inspector, rejected=rng.random() < rate)
				slot_status = "Built"
				frappe.db.set_value("Plant Schedule Slot", slot.name, "slot_status", slot_status, update_modified=False)
			frappe.db.set_value("Plant Schedule", schedule, "status", "Completed", update_modified=False)

	# -----------------------------------------------------------------------------------
	# Kyushu powertrains and intercompany supply
	# -----------------------------------------------------------------------------------
	def powertrain_need(self, day: dt.date) -> Counter:
		"""Powertrains Aichi needs for the next three working days beyond what it holds or has in transit."""
		horizon = add_days(day, 5)
		need: Counter = Counter()
		for wo in frappe.get_all("Work Order", filters={"automotive_plant": "AICHI-VA", "docstatus": 1,
				"status": ("in", OPEN_WO), "planned_start_date": ("<=", f"{horizon} 23:59:59"),
				"production_item": ("in", list(self.structure["variants"]))}, pluck="name"):
			for row in frappe.get_all("Work Order Item", filters={"parent": wo}, fields=["item_code", "required_qty",
					"consumed_qty"]):
				if row.item_code in self.structure["powertrains"]:
					need[row.item_code] += flt(row.required_qty) - flt(row.consumed_qty)
		for code in list(need):
			on_hand = sum(self.actual(code, w) for w in (store_for("AICHI-VA"), line_side_for("AICHI-FA1"),
				line_side_for("AICHI-FA2")))
			in_transit = flt(frappe.db.sql("""select sum(dni.qty) from `tabDelivery Note Item` dni
				join `tabDelivery Note` dn on dn.name = dni.parent where dn.docstatus = 1 and dn.company = %s
				and dni.item_code = %s and not exists (select 1 from `tabPurchase Receipt Item` pri join `tabPurchase Receipt` pr
				on pr.name = pri.parent where pr.docstatus = 1 and pr.inter_company_reference = dn.name)""",
				(KYUSHU, code))[0][0])
			need[code] = max(0.0, need[code] - on_hand - in_transit)
		return need

	def powertrains(self, day: dt.date) -> None:
		ctx = self.ctx
		batch = int(ctx.v("kyushu_powertrain_batch"))
		planner = self.sim.person("Production Planner", "KYUSHU-PT")
		supervisor = self.sim.person("Production Supervisor", "KYUSHU-PT")
		inspector = self.sim.person("Quality Inspector", "KYUSHU-PT")
		fg = fg_for("KYUSHU-PT")
		with ctx.step(7, 0):
			need = self.powertrain_need(day)
			for code, qty in sorted(need.items()):
				short = qty - self.actual(code, fg)
				if short <= 0:
					continue
				units = math.ceil(short / batch) * batch
				info = self.structure["powertrains"][code]
				if code.startswith("P180"):
					self._engines(units, planner, supervisor)
				else:
					self._machined("GEARSET-240", units * (2 if code == "E240-D" else 1), planner, supervisor)
					self._machined("HOUSING-240", units, planner, supervisor)
				source = store_for("KYUSHU-PT")
				for start in range(0, units, batch):
					qty = min(batch, units - start)
					wo = self.work_order(code, qty, KYUSHU, "KYUSHU-PT", info["line"], source, fg, ctx.clock.tick(60),
						planner)
					for _unit in range(qty):
						se = self.build(wo, 1, supervisor)
						self.sim.quality.powertrain_inspection(code, se, inspector)
		with ctx.step(7, 45):
			self.sim.sales.ship_powertrains(day)

	def _machined(self, code: str, units: int, planner: str, supervisor: str) -> None:
		store = store_for("KYUSHU-PT")
		short = units - self.actual(code, store)
		if short <= 0:
			return
		qty = math.ceil(short / 10) * 10
		wo = self.work_order(code, qty, KYUSHU, "KYUSHU-PT", "KYU-MACH", store, store, self.ctx.clock.tick(60), planner)
		self.build(wo, qty, supervisor)

	def _engines(self, units: int, planner: str, supervisor: str) -> None:
		store = store_for("KYUSHU-PT")
		short = units - self.actual("ENG-P180", store)
		if short <= 0:
			return
		self._machined("BLOCK-MACH", short, planner, supervisor)
		self._machined("HEAD-MACH", short, planner, supervisor)
		wo = self.work_order("ENG-P180", short, KYUSHU, "KYUSHU-PT", "KYU-P180", store, store, self.ctx.clock.tick(60),
			planner)
		for _unit in range(int(short)):
			self.build(wo, 1, supervisor)

	# -----------------------------------------------------------------------------------
	# shift handovers
	# -----------------------------------------------------------------------------------
	def handovers(self, day: dt.date) -> None:
		"""Each shift's supervisor hands the line over to the next shift's, who acknowledges it
		(ERP-04.5); the shift's results are computed from its transactions on submit."""
		from automotive_manufacturing.shop_floor.handover import acknowledge

		ctx = self.ctx
		for line in frappe.get_all("Production Line", filters={"plant": ("in", ("AICHI-VA", "KYUSHU-PT"))},
				fields=["name", "plant", "shift_pattern"], order_by="name"):
			supervisors = frappe.db.sql("""select distinct e.user_id from `tabPlant Assignment` pa
				join `tabEmployee` e on e.name = pa.employee join `tabHas Role` r on r.parent = e.user_id
				and r.role = 'Production Supervisor' where pa.production_line = %s and pa.status = 'Active'
				order by e.user_id""", line.name, pluck=True)
			if len(supervisors) < 2:  # a small line: the plant's supervisors cover it
				supervisors = sorted({self.sim.person("Production Supervisor", line.plant, i) for i in range(3)})
			if len(supervisors) < 2:
				continue
			shifts = [row.shift_code for row in frappe.get_cached_doc("Shift Pattern", line.shift_pattern).shifts]
			for i, code in enumerate(shifts):
				outgoing, incoming = supervisors[i % len(supervisors)], supervisors[(i + 1) % len(supervisors)]
				with ctx.step(23, 50):
					with ctx.as_user(outgoing):
						doc = frappe.get_doc({"doctype": "Shift Handover", "plant": line.plant, "production_line": line.name,
							"shift_date": day, "shift_code": code, "incoming_supervisor": incoming,
							"handover_notes": f"Shift {code} on {line.name}: results and open items as reported.",
							"safety_notes": "No incidents."}).insert()
						doc.submit()
					with ctx.as_user(incoming):
						acknowledge(doc.name)
				ctx.count("Shift Handover")

	# -----------------------------------------------------------------------------------
	# engineering changes
	# -----------------------------------------------------------------------------------
	# share of engineering changes whose quality sign-off waits two days (and so escalates)
	ECO_LATE_SIGN_OFF = 0.35

	def engineering(self, day: dt.date) -> None:
		ctx = self.ctx
		self.eco_sign_offs(day)
		rng = ctx.rng("eco", day)
		if rng.random() > float(ctx.v("ecos_per_month")) / 21.0:
			return
		pending = {p["module"] for p in self.s.get("eco_pending", {}).values()}
		modules = sorted(m for m in self.structure["modules"] if m.startswith(("FAST-KIT", "SEAT-", "DOOR-"))
			and m not in pending)
		target_item = rng.choice(modules)
		bom = frappe.db.get_value("BOM", {"item": target_item, "docstatus": 1, "is_active": 1,
			"automotive_superseded_by": ("is", "not set")}, "name", order_by="creation desc")
		if not bom:
			return
		lines = frappe.get_all("BOM Item", filters={"parent": bom}, fields=["item_code", "qty"])
		line = rng.choice(lines)
		engineer = self.sim.person("Product Engineer", "AICHI-VA")
		factor = rng.choice((0.9, 1.1, 1.05))
		uom = frappe.get_cached_value("Item", line.item_code, "stock_uom")
		if frappe.get_cached_value("UOM", uom, "must_be_whole_number"):
			# whole parts: one more, or one fewer while at least one remains
			new_qty = flt(line.qty) + 1 if factor > 1 or flt(line.qty) <= 1 else flt(line.qty) - 1
		else:
			new_qty = round(flt(line.qty) * factor, 3) or flt(line.qty)
		change = {"change_type": "Change Quantity", "target_bom": bom, "component": line.item_code,
			"qty": new_qty, "reason": "Design optimisation"}
		if target_item.startswith("FAST-KIT"):
			family = line.item_code.rsplit("-", 1)[0]
			alternatives = sorted(f for f in self.structure.get("fasteners", []) if f.startswith(family) and f != line.item_code
				and f not in {x.item_code for x in lines})
			if alternatives:
				change = {"change_type": "Replace Component", "target_bom": bom, "component": line.item_code,
					"new_component": rng.choice(alternatives), "reason": "Fastener standardisation"}
		with ctx.step(18, 0):
			with ctx.as_user(engineer):
				eco = frappe.get_doc({"doctype": "Engineering Change Order", "title": f"{target_item}: {change['reason']}",
					"company": AICHI, "effective_date": add_days(day, 7), "changes": [change]}).insert()
				req = approvals.request_approval(eco.doctype, eco.name)
			with ctx.as_user(self.sim.person("Engineering Manager", "AICHI-VA")):
				approvals.decide(req["name"], "Approve")
			if rng.random() < self.ECO_LATE_SIGN_OFF:
				# quality signs off two days later; the request escalates in the meantime
				self.s.setdefault("eco_pending", {})[eco.name] = {"request": req["name"], "engineer": engineer,
					"module": target_item, "due": str(add_days(day, 2))}
			else:
				with ctx.as_user(self.sim.person("Quality Manager", "AICHI-VA")):
					approvals.decide(req["name"], "Approve")
				with ctx.as_user(engineer):
					eco.reload()
					eco.submit()
		ctx.count("Engineering Change Order")

	def eco_sign_offs(self, day: dt.date) -> None:
		"""The quality sign-offs that waited: decided in the morning they are due, then the
		engineer submits the change."""
		ctx = self.ctx
		for name, pending in sorted(self.s.get("eco_pending", {}).items()):
			if getdate(pending["due"]) > day:
				continue
			with ctx.step(9, 0):
				with ctx.as_user(self.sim.person("Quality Manager", "AICHI-VA")):
					approvals.decide(pending["request"], "Approve")
				with ctx.as_user(pending["engineer"]):
					frappe.get_doc("Engineering Change Order", name).submit()
			self.s["eco_pending"].pop(name)

	def escalations(self, day: dt.date) -> None:
		"""The hourly escalation job, once each working evening: approvals waiting past
		their step's escalation time are escalated (automotive_core.governance.approvals)."""
		ctx = self.ctx
		with ctx.step(19, 0):
			for _request in approvals.escalate_overdue():
				ctx.count("Approval Escalation")
