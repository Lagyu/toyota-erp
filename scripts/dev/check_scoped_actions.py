#!/usr/bin/env python3
"""Whitelisted actions that write a document past the DocType permissions check the caller's
scope first (SEC-3, ops/security/findings.md C-10).

A whitelisted function that loads a document by name (``frappe.get_doc("<DocType>", name)``)
and then writes past the permissions (``ignore_permissions``, ``db_set``,
``frappe.db.set_value``, a service transition) must load it with
``plant_scope.get_scoped_doc``, check it itself (``check_permission``, ``has_permission``), or
scope it to a partner (the portal helpers). Reviewed exceptions are listed in REVIEWED with
the reason. Pure ``ast``: run by scripts/ci/lint.sh.

	python3 scripts/dev/check_scoped_actions.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPS = ("automotive_core", "automotive_manufacturing", "automotive_supply_chain", "automotive_portals")
WRITES = ("ignore_permissions=True", "ignore_permissions = True", "flags.ignore_permissions", ".db_set(",
	"frappe.db.set_value(", "_transition(")
CHECKS = ("get_scoped_doc", "check_permission", "has_permission", "require_party", "require_capability",
	"assert_party_access", "_supplier(", "_dealer(")
REVIEWED = {
	"automotive_core.governance.approvals.decide":
		"the approval service decides who may act: the step's approvers and their delegates (resolve_authority)",
	"automotive_core.governance.notifications.set_my_preference": "the caller's own preference only",
	"automotive_core.integration_hub.inbound.replay": "IT operators only; integration messages are group-wide",
	"automotive_core.integration_hub.outbox.retry": "IT operators only; integration messages are group-wide",
}


def unchecked() -> list[str]:
	found = []
	for app in APPS:
		base = ROOT / "apps" / app
		for path in sorted((base / app).rglob("*.py")):
			rel = path.relative_to(base)
			if "tests" in rel.parts or "patches" in rel.parts:
				continue
			module = ".".join(rel.with_suffix("").parts)
			for fn in ast.parse(path.read_text(), filename=str(path)).body:
				if not isinstance(fn, ast.FunctionDef) or not any(
						ast.unparse(d.func if isinstance(d, ast.Call) else d) == "frappe.whitelist" for d in fn.decorator_list):
					continue
				text = ast.unparse(fn)
				loads = any(isinstance(n, ast.Call) and ast.unparse(n.func) == "frappe.get_doc" and len(n.args) >= 2
					and isinstance(n.args[0], ast.Constant) for n in ast.walk(fn))
				if loads and any(w in text for w in WRITES) and not any(c in text for c in CHECKS):
					found.append(f"{module}.{fn.name}")
	return found


def main() -> int:
	found = unchecked()
	new = [name for name in found if name not in REVIEWED]
	stale = sorted(set(REVIEWED) - set(found))
	for name in new:
		print(f"{name}: writes a loaded document past the permissions without checking the caller's scope "
			"(use plant_scope.get_scoped_doc, or review it into REVIEWED)", file=sys.stderr)
	for name in stale:
		print(f"{name}: listed in REVIEWED but no longer needs it", file=sys.stderr)
	print(f"scoped actions: {len(found) - len(new)} reviewed exception(s), {len(new)} unchecked, {len(stale)} stale")
	return 1 if new or stale else 0


if __name__ == "__main__":
	sys.exit(main())
