"""The contract versioning check of openapi.py (D-034): which changes are breaking, and the
version each kind of change needs. Pure Python: run by scripts/ci/unit.sh."""

from __future__ import annotations

import copy
import io
import unittest
from contextlib import redirect_stdout

import openapi

PATH = "/api/method/automotive_portals.api.dealer.place_order"


def contract(version: str = "1.0.0") -> dict:
	body = {
		"type": "object",
		"required": ["vehicle_model"],
		"properties": {"vehicle_model": {"type": "string"}, "qty": {"type": "integer", "default": 1}},
	}
	return {
		"info": {"version": version},
		"components": {
			"parameters": {
				"IdempotencyKey": {
					"name": "Idempotency-Key",
					"in": "header",
					"required": True,
					"schema": {"type": "string"},
				},
				"IdempotencyKeyOptional": {
					"name": "Idempotency-Key",
					"in": "header",
					"required": False,
					"schema": {"type": "string"},
				},
			}
		},
		"paths": {
			PATH: {
				"post": {
					"summary": "Place an order",
					"parameters": [{"$ref": "#/components/parameters/IdempotencyKeyOptional"}],
					"requestBody": {"required": True, "content": {"application/json": {"schema": body}}},
					"responses": {
						"200": {
							"description": "OK",
							"content": {
								"application/json": {
									"schema": {
										"type": "object",
										"properties": {
											"message": {
												"type": "object",
												"properties": {
													"name": {"type": "string"},
													"status": {"type": "string"},
												},
											}
										},
									}
								}
							},
						}
					},
				}
			}
		},
	}


def check(old: dict, new: dict) -> tuple[list[str], list[str]]:
	with redirect_stdout(io.StringIO()):
		return openapi.compare(old, new), openapi.compat(old, new)


class TestContractVersions(unittest.TestCase):
	def test_unchanged_or_reworded_needs_no_new_version(self):
		new = contract()
		new["paths"][PATH]["post"]["summary"] = "Place an order for vehicles"
		(breaking, other), problems = check(contract(), new)
		self.assertEqual((breaking, other, problems), ([], [], []))

	def test_an_addition_needs_a_new_minor_version(self):
		new = contract()
		new["paths"][PATH]["post"]["requestBody"]["content"]["application/json"]["schema"]["properties"][
			"note"
		] = {"type": "string"}
		new["paths"]["/api/method/automotive_portals.api.dealer.orders"] = {"get": {"responses": {}}}
		(breaking, other), problems = check(contract(), new)
		self.assertEqual(breaking, [])
		self.assertEqual(len(other), 2)
		self.assertEqual(len(problems), 1)
		new["info"]["version"] = "1.1.0"
		self.assertEqual(check(contract(), new)[1], [])

	def test_breaking_changes_need_a_new_major_version(self):
		cases = {
			"removed": lambda c: c["paths"].pop(PATH),
			"newly required": lambda c: c["paths"][PATH]["post"]["requestBody"]["content"][
				"application/json"
			]["schema"]["required"].append("qty"),
			"retyped": lambda c: c["paths"][PATH]["post"]["requestBody"]["content"]["application/json"][
				"schema"
			]["properties"].update(qty={"type": "string"}),
			"result property removed": lambda c: c["paths"][PATH]["post"]["responses"]["200"]["content"][
				"application/json"
			]["schema"]["properties"]["message"]["properties"].pop("status"),
			"key now required": lambda c: c["paths"][PATH]["post"].update(
				parameters=[{"$ref": "#/components/parameters/IdempotencyKey"}]
			),
		}
		for case, change in cases.items():
			with self.subTest(case):
				new = copy.deepcopy(contract("1.4.0"))
				change(new)
				(breaking, _), problems = check(contract(), new)
				self.assertEqual(len(breaking), 1, breaking)
				self.assertEqual(len(problems), 1, problems)  # a minor version is not enough
				new["info"]["version"] = "2.0.0"
				self.assertEqual(check(contract(), new)[1], [])

	def test_withdrawing_guest_access_is_breaking(self):
		old = contract()
		old["paths"][PATH]["post"]["security"] = []
		(breaking, _), _ = check(old, contract())
		self.assertEqual(breaking, [f"POST {PATH}: no longer open to guests"])

	def test_a_lower_version_is_refused(self):
		self.assertEqual(len(check(contract("1.2.0"), contract("1.1.9"))[1]), 1)


if __name__ == "__main__":
	unittest.main()
