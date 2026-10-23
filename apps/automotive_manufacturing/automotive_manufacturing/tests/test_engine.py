"""Pure algorithm tests (no database): VIN, option rules, sequencing, MRP netting.

Runnable both by `bench run-tests` and by plain pytest/unittest on the host.
"""

from __future__ import annotations

import copy
import unittest

from automotive_manufacturing.engine import mrp, options, sequencing, vin


class TestVin(unittest.TestCase):
	def test_known_check_digit(self):
		# Worked example from 49 CFR 565 / common references
		self.assertEqual(vin.check_digit("1M8GDM9A_KP042788".replace("_", "0")), "X")
		self.assertTrue(vin.is_valid("1M8GDM9AXKP042788"))

	def test_build_round_trip(self):
		v = vin.build("JX9H1C2E", 2026, "A", 123)
		self.assertEqual(len(v), 17)
		self.assertTrue(vin.is_valid(v))
		self.assertEqual(v[9], "T")  # 2026 model year
		self.assertEqual(v[10:], "A000123")
		self.assertFalse(vin.is_valid(v[:8] + ("0" if v[8] != "0" else "1") + v[9:]))

	def test_rejects_forbidden_letters(self):
		with self.assertRaises(vin.VinError):
			vin.build("JX9H1COE", 2026, "A", 1)  # letter O


class TestOptionRules(unittest.TestCase):
	RULES = [
		options.Rule("R1", "Requires", "drivetrain", frozenset({"AWD"}), "powertrain", frozenset({"EV-DUAL"})),
		options.Rule("R2", "Excludes", "trim", frozenset({"BASE"}), "package", frozenset({"TECH"}),
			message="Tech package needs a higher trim"),
		options.Rule("R3", "Allowed Values", "market", frozenset({"JP", "US"})),
	]

	def test_valid_configuration(self):
		sel = {"market": "JP", "powertrain": "EV-DUAL", "drivetrain": "AWD", "trim": "PREMIUM", "package": "TECH"}
		self.assertEqual(options.evaluate(sel, self.RULES), [])

	def test_each_rule_type_reports(self):
		sel = {"market": "EU", "powertrain": "EV-DUAL", "drivetrain": "FWD", "trim": "BASE", "package": "TECH"}
		codes = [v.rule for v in options.evaluate(sel, self.RULES, required=["color"])]
		self.assertEqual(codes, ["required", "R1", "R2", "R3"])
		self.assertIn("Tech package", [v.message for v in options.evaluate(sel, self.RULES)][1])

	def test_key_is_order_independent(self):
		self.assertEqual(options.configuration_key("H2", {"b": "2", "a": "1"}),
			options.configuration_key("H2", {"a": "1", "b": "2"}))


class TestSequencing(unittest.TestCase):
	def test_goal_chasing_levels_the_mix(self):
		units = [sequencing.Unit(f"A{i}", "H1-FWD") for i in range(6)] + [
			sequencing.Unit(f"B{i}", "H2-AWD") for i in range(3)] + [sequencing.Unit("C0", "H1-AWD")]
		seq = sequencing.goal_chasing(units)
		self.assertEqual(len(seq), 10)
		self.assertEqual(sorted(u.ref for u in seq), sorted(u.ref for u in units))
		keys = [u.mix_key for u in seq]
		self.assertNotEqual(keys[:6], ["H1-FWD"] * 6)  # not batched
		batched = sorted(units, key=lambda u: u.mix_key)
		self.assertLess(sequencing.leveling_deviation(seq), sequencing.leveling_deviation(batched))
		self.assertEqual(seq, sequencing.goal_chasing(list(reversed(units))))  # deterministic


def base_snapshot():
	return {
		"as_of": "2026-04-06",
		"parameters": {"horizon_days": 30, "frozen_zone_days": 2, "lot_sizing": "Minimum Order Qty Multiples",
			"capacity_check": True},
		"working_days": [f"2026-04-{d:02d}" for d in (6, 7, 8, 9, 10, 13, 14, 15, 16, 17, 20, 21, 22, 23, 24)],
		"items": {
			"CAR": {"lead_time_days": 2, "is_purchased": False, "line": "FA", "uom": "Nos"},
			"SEAT": {"lead_time_days": 1, "is_purchased": False, "uom": "Nos"},
			"FOAM": {"lead_time_days": 5, "is_purchased": True, "supplier": "Foam KK", "min_order_qty": 50, "uom": "Kg"},
			"BOLT": {"lead_time_days": 3, "is_purchased": True, "supplier": "Bolt KK", "safety_stock": 100, "uom": "Nos"},
		},
		"boms": {
			"BOM-CAR-A": {"item": "CAR", "quantity": 1, "is_default": 1, "effective_to": "2026-04-14",
				"lines": [{"item_code": "SEAT", "qty": 2}, {"item_code": "BOLT", "qty": 10}]},
			"BOM-CAR-B": {"item": "CAR", "quantity": 1, "effective_from": "2026-04-15",
				"lines": [{"item_code": "SEAT", "qty": 2}, {"item_code": "BOLT", "qty": 12}]},
			"BOM-SEAT": {"item": "SEAT", "quantity": 1, "is_default": 1, "lines": [{"item_code": "FOAM", "qty": 1.5}]},
		},
		"on_hand": {"BOLT": 150, "SEAT": 4},
		"scheduled_receipts": [{"item_code": "FOAM", "qty": 20, "date": "2026-04-08", "source": "PO-1"}],
		"demand": [{"item_code": "CAR", "qty": 10, "date": "2026-04-13", "source": "SO-1"},
			{"item_code": "CAR", "qty": 5, "date": "2026-04-20", "source": "SO-2"}],
		"lines": {"FA": {"daily_capacity": 8}},
	}


class TestMrp(unittest.TestCase):
	def test_netting_explosion_effectivity_and_lot_sizing(self):
		out = mrp.plan(base_snapshot())
		cars = [p for p in out["proposals"] if p["item_code"] == "CAR"]
		self.assertEqual([(p["qty"], p["need_date"], p["release_date"], p["bom_no"]) for p in cars],
			[(10, "2026-04-13", "2026-04-09", "BOM-CAR-A"), (5, "2026-04-20", "2026-04-16", "BOM-CAR-B")])
		seats = [p for p in out["proposals"] if p["item_code"] == "SEAT"]
		self.assertEqual(sum(p["qty"] for p in seats), 30 - 4)  # 15 cars x 2 seats, net of 4 on hand
		bolt = [p for p in out["proposals"] if p["item_code"] == "BOLT"]
		# 10x10 + 5x12 = 160 needed, 150 on hand, safety 100 -> 110 short
		self.assertEqual(sum(p["qty"] for p in bolt), 110)
		foam = [p for p in out["proposals"] if p["item_code"] == "FOAM"]
		self.assertTrue(all(p["qty"] % 50 == 0 for p in foam))  # MOQ multiples
		self.assertEqual(foam[0]["supplier"], "Foam KK")

	def test_lead_time_and_capacity_exceptions(self):
		snapshot = base_snapshot()
		snapshot["demand"].append({"item_code": "CAR", "qty": 9, "date": "2026-04-07", "source": "SO-RUSH"})
		out = mrp.plan(snapshot)
		causes = {(s["item_code"], s["cause"]) for s in out["shortages"]}
		self.assertIn(("CAR", "Lead Time"), causes)
		self.assertIn(("line:FA", "Capacity"), causes)  # 9 rush units released today > 8/day
		self.assertTrue(any(c["conflict"] for c in out["capacity"]))

	def test_replay_is_identical(self):
		a = mrp.plan(base_snapshot())
		b = mrp.plan(copy.deepcopy(base_snapshot()))
		self.assertEqual(a, b)

	def test_bom_cycle_is_rejected(self):
		snapshot = base_snapshot()
		snapshot["boms"]["BOM-FOAM"] = {"item": "FOAM", "quantity": 1, "lines": [{"item_code": "CAR", "qty": 1}]}
		with self.assertRaises(ValueError):
			mrp.plan(snapshot)


if __name__ == "__main__":
	unittest.main()
