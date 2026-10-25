"""Quality flows: final/powertrain/incoming inspections, rework of rejected vehicles,
Andon events with containment and restart, nonconformance closure with CAPA."""

from __future__ import annotations

import datetime as dt
import random

import frappe
from frappe.utils import add_days, flt

from automotive_manufacturing.quality_control import holds
from automotive_manufacturing.quality_control.inspections import make_reinspection
from automotive_manufacturing.shop_floor import andon as andon_service


class Quality:
	def __init__(self, sim):
		self.sim = sim
		self.ctx = sim.ctx
		self.s = sim.state["sim"]

	def readings(self, template: str, rejected: bool, key: str, samples: int = 1) -> list[dict]:
		"""Readings for an inspection plan: every measurement inside its range (away from the
		limits), or, for a rejection, one parameter out of tolerance on the first sample."""
		rng = random.Random(f"{self.ctx.seed}:qi:{key}")
		parameters = frappe.get_all(
			"Item Quality Inspection Parameter",
			filters={"parent": template, "parenttype": "Quality Inspection Template"},
			fields=["specification", "numeric", "min_value", "max_value", "value"],
			order_by="idx",
		)
		failing = rng.randrange(len(parameters)) if rejected and parameters else None
		rows = []
		for i, p in enumerate(parameters):
			row = {
				"specification": p.specification,
				"numeric": p.numeric,
				"min_value": p.min_value,
				"max_value": p.max_value,
				"value": p.value,
			}
			if p.numeric:
				low, high = flt(p.min_value), flt(p.max_value)
				span = high - low
				for n in range(1, samples + 1):
					x = rng.uniform(low + 0.15 * span, high - 0.15 * span)
					if i == failing and n == 1:
						x = high + span * rng.uniform(0.05, 0.3)
					row[f"reading_{n}"] = f"{x:.3f}"
			else:
				row["reading_value"] = "NG" if i == failing else p.value
			rows.append(row)
		return rows

	def _qi(
		self,
		item: str,
		reference_type: str,
		reference: str,
		stage: str,
		inspector: str,
		status: str,
		company: str,
		serial: str | None = None,
		batch: str | None = None,
		remarks: str | None = None,
		child_row: str | None = None,
		template: str | None = None,
		sample_size: int = 1,
	) -> str:
		"""child_row: inspect one unit of a multi-unit row (ERPNext otherwise allows one inspection per row).
		Items with an inspection plan get readings and ERPNext decides the status from them."""
		template = template or frappe.get_cached_value("Item", item, "quality_inspection_template")
		values = {
			"doctype": "Quality Inspection",
			"inspection_type": "Incoming" if stage == "Incoming" else "In Process",
			"automotive_inspection_stage": stage,
			"reference_type": reference_type,
			"reference_name": reference,
			"child_row_reference": child_row,
			"item_code": item,
			"item_serial_no": serial,
			"batch_no": batch,
			"sample_size": sample_size,
			"inspected_by": inspector,
			"company": company,
			"remarks": remarks,
		}
		if template:
			values.update(
				{
					"quality_inspection_template": template,
					"manual_inspection": 0,
					"readings": self.readings(
						template,
						status == "Rejected",
						f"{reference}:{item}:{serial or batch or ''}",
						min(sample_size, 3),
					),
				}
			)
		else:
			values.update({"status": status, "manual_inspection": 1})
		with self.ctx.as_user(inspector):
			qi = frappe.get_doc(values)
			qi.insert()
			if qi.status != status:
				raise RuntimeError(f"inspection {qi.name}: readings gave {qi.status}, expected {status}")
			qi.submit()
		self.ctx.count("Quality Inspection")
		return qi.name

	def final_inspection(self, vin: str, stock_entry: str, inspector: str, rejected: bool) -> None:
		variant = frappe.db.get_value("Vehicle Unit", vin, "variant_item")
		company = frappe.db.get_value("Vehicle Unit", vin, "company")
		status = "Rejected" if rejected else "Accepted"
		qi = self._qi(
			variant,
			"Stock Entry",
			stock_entry,
			"Final",
			inspector,
			status,
			company,
			serial=vin,
			remarks="Water leak at rear hatch seal" if rejected else None,
		)
		if rejected:
			self.s.setdefault("rework", []).append({"vin": vin, "entry": stock_entry, "inspection": qi})

	def powertrain_inspection(self, item: str, stock_entry: str, inspector: str) -> None:
		rows = frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": stock_entry, "is_finished_item": 1},
			fields=["serial_no"],
			order_by="idx asc",
		)
		serial = (rows[0].serial_no or "").split("\n")[0] if rows else None
		self._qi(
			item,
			"Stock Entry",
			stock_entry,
			"Final",
			inspector,
			"Accepted",
			"Hinode Powertrain Components",
			serial=serial or None,
		)

	def incoming(self, item: str, receipt: str, batch: str | None, plant: str, rejected: bool) -> str:
		inspector = self.sim.person("Quality Inspector", plant)
		company = frappe.get_cached_value("Plant", plant, "company")
		return self._qi(
			item,
			"Purchase Receipt",
			receipt,
			"Incoming",
			inspector,
			"Rejected" if rejected else "Accepted",
			company,
			batch=batch,
			remarks="Out of tolerance on the first sample" if rejected else None,
			sample_size=3,
		)

	# -----------------------------------------------------------------------------------
	def daily(self, day: dt.date) -> None:
		"""Rework yesterday's rejected vehicles, then release them after re-inspection."""
		ctx = self.ctx
		pending = self.s.get("rework", [])
		keep = []
		qm = [
			u
			for u in self.sim.roster.get("Quality Manager", [])
			if ctx.cache.get("plant_of", {}).get(u) == "AICHI-VA"
		]
		for item in pending:
			vin = item["vin"]
			hold = frappe.db.get_value(
				"Quality Hold",
				{"serial_no": vin, "status": "Active"},
				"name",
				order_by="creation desc, name desc",
			)
			if not hold:
				continue
			with ctx.step(18, 30):
				doc = frappe.get_doc("Quality Hold", hold)
				raiser = doc.raised_by
				releaser = next((u for u in qm if u != raiser), qm[0] if qm else "Administrator")
				for check in doc.release_checks:
					check.done = 1
				with ctx.as_user(releaser):
					doc.save()
					inspector = self.sim.person("Quality Inspector", "AICHI-VA", 1)
					with ctx.as_user(inspector):
						rejected_qi = item.get("inspection") or frappe.db.get_value(
							"Quality Inspection",
							{"item_serial_no": vin, "status": "Rejected", "docstatus": 1},
							"name",
							order_by="creation desc, name desc",
						)
						template = frappe.db.get_value(
							"Quality Inspection", rejected_qi, "quality_inspection_template"
						)
						reinspection = frappe.get_doc(
							"Quality Inspection",
							make_reinspection(
								rejected_qi,
								remarks="Re-inspection after rework: seal replaced, water test passed",
								readings=self.readings(template, False, f"rework:{rejected_qi}")
								if template
								else None,
							),
						)
						reinspection.submit()
					ctx.count("Quality Inspection")
					holds.release(hold, "Seal replaced and water test passed")
				self._close_nc(doc, releaser)
		self.s["rework"] = keep

	def _close_nc(self, hold, manager: str) -> None:
		nc_name = frappe.db.get_value("Non Conformance", {"automotive_quality_hold": hold.name}, "name")
		if not nc_name:
			return
		with self.ctx.as_user(manager):
			action = frappe.get_doc(
				{
					"doctype": "Quality Action",
					"corrective_preventive": "Corrective",
					"status": "Completed",
					"date": frappe.utils.nowdate(),
					"resolutions": [
						{
							"problem": "Hatch seal leak",
							"resolution": "Seal fitting fixture recalibrated",
							"status": "Completed",
						}
					],
				}
			)
			action.insert(ignore_permissions=True)
			nc = frappe.get_doc("Non Conformance", nc_name)
			nc.corrective_action = nc.corrective_action or "Rework: seal replaced, water test repeated"
			nc.automotive_root_cause_method = "5 Why"
			nc.automotive_root_cause = "Seal fitting fixture out of calibration"
			nc.automotive_capa = action.name
			nc.status = "Resolved"
			if nc.owner == manager:
				return  # the reporter cannot approve closure; another manager will
			nc.save(ignore_permissions=True)

	# -----------------------------------------------------------------------------------
	def andon(self, day: dt.date) -> None:
		ctx = self.ctx
		rng = ctx.rng("andon", day)
		per_day = float(ctx.v("andon_events_per_week_per_plant")) / 5.0
		for plant, lines in (
			("AICHI-VA", ["AICHI-FA1", "AICHI-FA2", "AICHI-BODY"]),
			("KYUSHU-PT", ["KYU-P180", "KYU-E240"]),
		):
			if rng.random() > per_day:
				continue
			line = rng.choice(lines)
			stations = frappe.get_all(
				"Production Line Station", filters={"parent": line}, pluck="workstation", order_by="idx asc"
			)
			reason = rng.choice(["E-BREAKDOWN", "E-TOOL", "M-SHORT", "P-OVERCYCLE", "Q-GAUGE", "S-NEAR-MISS"])
			stop_type = "Call" if reason in ("P-OVERCYCLE", "S-NEAR-MISS") else "Line Stop"
			operator = self.sim.person("Line Operator", plant, rng.randint(0, 50))
			supervisor = self.sim.person("Production Supervisor", plant, rng.randint(0, 50))
			hour = rng.randint(9, 15)
			with ctx.step(hour, rng.randint(0, 59)):
				with ctx.as_user(operator):
					event = frappe.get_doc(
						{
							"doctype": "Andon Event",
							"production_line": line,
							"workstation": rng.choice(stations),
							"reason_code": reason,
							"stop_type": stop_type,
							"description": "Raised from the station andon cord",
						}
					).insert()
				ctx.count("Andon Event")
			minutes = rng.randint(4, 38)
			with ctx.frozen(ctx.clock.tick(60)):
				with ctx.as_user(supervisor):
					andon_service.acknowledge(event.name)
					andon_service.contain(event.name, "Station isolated, team leader on site")
			with ctx.frozen(ctx.clock.tick(minutes * 60)):
				event.reload()
				if event.quality_hold:
					hold = frappe.get_doc("Quality Hold", event.quality_hold)
					for check in hold.release_checks:
						check.done = 1
					managers = [
						u
						for u in self.sim.roster.get("Quality Manager", [])
						if ctx.cache.get("plant_of", {}).get(u) == plant
					]
					with ctx.as_user(managers[0]):
						hold.save()
						holds.release(hold.name, "Gauge re-verified, last parts re-checked")
					restarter = managers[0]
				else:
					restarter = supervisor
				with ctx.as_user(restarter):
					andon_service.authorize_restart(event.name, "Containment verified")
				with ctx.as_user(supervisor):
					andon_service.close(event.name)
