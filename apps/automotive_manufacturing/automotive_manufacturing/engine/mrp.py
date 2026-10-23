"""Deterministic material requirements planning over a recorded snapshot (ERP-03).

``plan(snapshot)`` is a pure function: the same snapshot (inputs + versioned
parameters) always yields the same proposals, shortages and capacity load, so any
Planning Run can be replayed and verified by hash. No Frappe imports.

Snapshot shape (all dates ISO strings)::

    {
      "as_of": "2026-04-01",
      "parameters": {"horizon_days": 56, "frozen_zone_days": 3, "lot_sizing": "Lot For Lot",
                     "safety_stock_policy": "Item Safety Stock", "days_of_cover": 0,
                     "lead_time_source": "Item Lead Time", "fixed_lead_time_days": 0,
                     "include_kanban_items": false, "capacity_check": true},
      "working_days": ["2026-04-01", ...],          # plant calendar inside the horizon
      "items": {code: {"lead_time_days", "safety_stock", "min_order_qty", "is_purchased",
                       "supplier", "uom", "warehouse", "line", "kanban", "daily_usage"}},
      "boms": {bom_no: {"item", "quantity", "effective_from", "effective_to", "is_default",
                        "lines": [{"item_code", "qty"}]}},
      "on_hand": {code: qty},
      "scheduled_receipts": [{"item_code", "qty", "date", "source"}],
      "demand": [{"item_code", "qty", "date", "source"}],
      "lines": {line: {"daily_capacity": units}}
    }
"""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from typing import Any

EPS = 1e-9


def _d(value: str | dt.date) -> dt.date:
	return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value)[:10])


def _r(value: float) -> float:
	return round(value + 0.0, 6)


class Calendar:
	def __init__(self, working_days: list[str]):
		self.days = sorted(_d(d) for d in working_days)
		self.set = set(self.days)

	def is_working(self, day: dt.date) -> bool:
		return day in self.set if self.days else day.weekday() < 5

	def subtract_working_days(self, day: dt.date, n: int) -> dt.date:
		"""Step back n working days. Before the recorded calendar, weekdays count as working."""
		current = day
		while n > 0:
			current -= dt.timedelta(days=1)
			known = bool(self.days) and current >= self.days[0]
			if (self.is_working(current) if known else current.weekday() < 5):
				n -= 1
		return current


def low_level_codes(items: dict[str, Any], boms: dict[str, Any]) -> dict[str, int]:
	children: dict[str, set[str]] = defaultdict(set)
	for bom in boms.values():
		for line in bom["lines"]:
			children[bom["item"]].add(line["item_code"])
	levels: dict[str, int] = {}

	def visit(item: str, depth: int, path: frozenset[str]) -> None:
		if item in path:
			raise ValueError(f"BOM cycle through {item}")
		if levels.get(item, -1) >= depth:
			return
		levels[item] = depth
		for child in sorted(children.get(item, ())):
			visit(child, depth + 1, path | {item})

	_assert_acyclic(children)
	parents = {c for kids in children.values() for c in kids}
	for root in sorted(set(items) | set(children)):
		if root not in parents:
			visit(root, 0, frozenset())
	for item in items:
		levels.setdefault(item, 0)
	return levels


def _assert_acyclic(children: dict[str, set[str]]) -> None:
	white, grey, black = 0, 1, 2
	colour: dict[str, int] = defaultdict(int)
	for start in sorted(children):
		if colour[start] != white:
			continue
		stack = [(start, iter(sorted(children.get(start, ()))))]
		colour[start] = grey
		while stack:
			node, it = stack[-1]
			child = next(it, None)
			if child is None:
				colour[node] = black
				stack.pop()
			elif colour[child] == grey:
				raise ValueError(f"BOM cycle through {child}")
			elif colour[child] == white:
				colour[child] = grey
				stack.append((child, iter(sorted(children.get(child, ())))))


def bom_for(item: str, on: dt.date, boms_by_item: dict[str, list[tuple[str, dict]]]) -> tuple[str, dict] | None:
	candidates = []
	for name, bom in boms_by_item.get(item, []):
		start = _d(bom["effective_from"]) if bom.get("effective_from") else dt.date.min
		end = _d(bom["effective_to"]) if bom.get("effective_to") else dt.date.max
		if start <= on <= end:
			candidates.append((start, bool(bom.get("is_default")), name, bom))
	if not candidates:
		return None
	start, _default, name, bom = max(candidates, key=lambda c: (c[0], c[1], c[2]))
	return name, bom


def lot_size(need: float, item: dict[str, Any], policy: str) -> float:
	if policy == "Minimum Order Qty Multiples" and item.get("min_order_qty"):
		moq = float(item["min_order_qty"])
		return _r(math.ceil((need - EPS) / moq) * moq)
	return _r(math.ceil(need - EPS) if item.get("whole_units", True) else need)


def plan(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
	params = snapshot["parameters"]
	as_of = _d(snapshot["as_of"])
	horizon_end = as_of + dt.timedelta(days=int(params.get("horizon_days") or 56))
	frozen_until = as_of + dt.timedelta(days=int(params.get("frozen_zone_days") or 0))
	calendar = Calendar(snapshot.get("working_days") or [])
	items: dict[str, dict] = snapshot["items"]
	boms_by_item: dict[str, list[tuple[str, dict]]] = defaultdict(list)
	for name in sorted(snapshot["boms"]):
		bom = snapshot["boms"][name]
		boms_by_item[bom["item"]].append((name, bom))
	levels = low_level_codes(items, snapshot["boms"])

	gross: dict[str, list[tuple[dt.date, float, str]]] = defaultdict(list)
	for row in snapshot.get("demand", []):
		day = _d(row["date"])
		if day <= horizon_end:
			gross[row["item_code"]].append((max(day, as_of), float(row["qty"]), row.get("source", "demand")))
	receipts: dict[str, list[tuple[dt.date, float, str]]] = defaultdict(list)
	for row in snapshot.get("scheduled_receipts", []):
		receipts[row["item_code"]].append((max(_d(row["date"]), as_of), float(row["qty"]), row.get("source", "receipt")))

	proposals: list[dict[str, Any]] = []
	shortages: list[dict[str, Any]] = []
	line_load: dict[tuple[str, dt.date], float] = defaultdict(float)

	for item_code in sorted(levels, key=lambda c: (levels[c], c)):
		item = items.get(item_code)
		if item is None or (item.get("kanban") and not params.get("include_kanban_items")):
			continue
		safety = float(item.get("safety_stock") or 0)
		if params.get("safety_stock_policy") == "Days Of Cover":
			safety = float(item.get("daily_usage") or 0) * float(params.get("days_of_cover") or 0)
		events: dict[dt.date, dict[str, Any]] = defaultdict(lambda: {"in": 0.0, "out": 0.0, "sources": []})
		for day, qty, source in receipts.get(item_code, []):
			events[day]["in"] += qty
		for day, qty, source in gross.get(item_code, []):
			events[day]["out"] += qty
			events[day]["sources"].append(source)
		projected = float(snapshot.get("on_hand", {}).get(item_code, 0))
		pending_sources: list[str] = []
		for day in sorted(events):
			projected += events[day]["in"] - events[day]["out"]
			pending_sources.extend(events[day]["sources"])
			if projected >= safety - EPS:
				continue
			need = safety - projected
			qty = lot_size(need, item, params.get("lot_sizing", "Lot For Lot"))
			manufactured = not item.get("is_purchased")
			lead = _lead_time(item, params)
			release = (
				calendar.subtract_working_days(day, lead) if manufactured else day - dt.timedelta(days=lead)
			)
			proposal = {
				"proposal_type": "Manufacture" if manufactured else "Purchase",
				"item_code": item_code,
				"qty": qty,
				"uom": item.get("uom"),
				"need_date": day.isoformat(),
				"release_date": max(release, as_of).isoformat(),
				"warehouse": item.get("warehouse"),
				"bom_no": None,
				"supplier": item.get("supplier"),
				"bom_level": levels[item_code],
				"pegging": "; ".join(sorted(set(pending_sources)))[:500],
			}
			if release < as_of:
				shortages.append(_shortage(item_code, day, need, projected, as_of + dt.timedelta(days=lead),
					"Lead Time", f"expedite: order/start {(as_of - release).days} day(s) late"))
			elif manufactured and release < frozen_until:
				shortages.append(_shortage(item_code, day, need, projected, frozen_until, "Frozen Zone",
					"needs a start inside the frozen zone; replan or expedite"))
			if manufactured:
				chosen = bom_for(item_code, max(release, as_of), boms_by_item)
				if not chosen:
					shortages.append(_shortage(item_code, day, need, projected, None, "No BOM",
						"release a BOM before planning this item"))
				else:
					bom_no, bom = chosen
					proposal["bom_no"] = bom_no
					factor = qty / float(bom.get("quantity") or 1)
					start = max(release, as_of)
					for line in bom["lines"]:
						gross[line["item_code"]].append((start, _r(float(line["qty"]) * factor),
							f"{item_code}@{day.isoformat()}"))
					if item.get("line"):
						line_load[(item["line"], start)] += qty
			elif not item.get("supplier"):
				shortages.append(_shortage(item_code, day, need, projected, None, "No Supply Source",
					"assign an approved supplier"))
			proposals.append(proposal)
			projected += qty
			pending_sources = []

	capacity = []
	if params.get("capacity_check"):
		for (line, day) in sorted(line_load):
			daily = float(snapshot.get("lines", {}).get(line, {}).get("daily_capacity") or 0)
			available = daily if calendar.is_working(day) else 0.0
			required = line_load[(line, day)]
			capacity.append({
				"production_line": line,
				"bucket_date": day.isoformat(),
				"required_units": _r(required),
				"available_units": _r(available),
				"load_percent": _r(required / available * 100) if available else 100.0,
				"conflict": int(required > available + EPS),
			})
			if required > available + EPS:
				shortages.append(_shortage(f"line:{line}", day, required, available, None, "Capacity",
					"level the load to adjacent working days or add overtime"))
	proposals.sort(key=lambda p: (p["bom_level"], p["item_code"], p["need_date"]))
	shortages.sort(key=lambda s: (s["need_date"], s["item_code"], s["cause"]))
	return {"proposals": proposals, "shortages": shortages, "capacity": capacity}


def _lead_time(item: dict[str, Any], params: dict[str, Any]) -> int:
	if params.get("lead_time_source") == "Fixed Days":
		return int(params.get("fixed_lead_time_days") or 0)
	return int(item.get("lead_time_days") or 0)


def _shortage(item_code: str, day: dt.date, required: float, available: float, earliest, cause: str,
		action: str) -> dict[str, Any]:
	return {
		"item_code": item_code,
		"need_date": day.isoformat(),
		"required_qty": _r(required),
		"available_qty": _r(available),
		"shortage_qty": _r(max(required - max(available, 0), 0)),
		"earliest_supply_date": earliest.isoformat() if earliest else None,
		"cause": cause,
		"action": action,
	}
