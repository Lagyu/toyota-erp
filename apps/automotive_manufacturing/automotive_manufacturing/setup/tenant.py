"""Tenant extension (automotive_core.setup.tenant hook): Andon reason codes and the
approval policies for manufacturing documents."""

from __future__ import annotations

import frappe

from automotive_core.setup.tenant import approval_policy

ANDON_REASONS = [
	("Q-DEFECT", "Defect found at station", "Quality", 1, 1),
	("Q-GAUGE", "Measurement out of specification", "Quality", 1, 1),
	("Q-WRONG-PART", "Wrong or mixed part at line side", "Quality", 1, 1),
	("M-SHORT", "Material shortage at line side", "Material Shortage", 0, 1),
	("M-DAMAGED", "Damaged container / packaging", "Material Shortage", 0, 0),
	("E-BREAKDOWN", "Equipment breakdown", "Equipment", 0, 1),
	("E-TOOL", "Tool or fixture problem", "Equipment", 0, 1),
	("S-INJURY", "Safety incident", "Safety", 0, 1),
	("S-NEAR-MISS", "Safety near miss (call only)", "Safety", 0, 0),
	("P-OVERCYCLE", "Operation exceeds cycle time", "Process", 0, 0),
	("P-HELP", "Team member needs help (call only)", "Process", 0, 0),
]

POLICIES = [
	{"name": "Engineering Change Release", "doctype": "Engineering Change Order",
	 "steps": [{"step": 1, "name": "Engineering manager", "role": "Engineering Manager"},
		{"step": 2, "name": "Quality sign-off", "role": "Quality Manager"}]},
	{"name": "Quality Disposition Approval", "doctype": "Quality Disposition",
	 "steps": [{"step": 1, "name": "Quality manager", "role": "Quality Manager"}]},
	{"name": "Planning Parameter Release", "doctype": "Planning Parameter Set",
	 "steps": [{"step": 1, "name": "Plant director", "role": "Plant Director"}]},
]


def apply(definition, log) -> None:
	for code, description, category, raises_hold, stops in ANDON_REASONS:
		if frappe.db.exists("Andon Reason Code", code):
			continue
		if not log.change(f"andon reason {code}"):
			continue
		frappe.get_doc({"doctype": "Andon Reason Code", "reason_code": code, "description": description,
			"category": category, "raises_quality_hold": raises_hold, "stops_line": stops}).insert(ignore_permissions=True)
	for policy in POLICIES:
		approval_policy(policy, log)
