"""Integrity checks run on a freshly restored copy of a site (ops/backup/restore-drill.sh).

They prove a backup is usable, not just present: the ledgers balance, the audit hash
chain verifies end to end, and the core record counts are there.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import flt

COUNTED = ("Company", "User", "Item", "BOM", "Work Order", "Purchase Order", "Sales Invoice", "GL Entry",
	"Stock Ledger Entry", "Audit Event", "Vehicle Unit")


def verify_restored_site() -> str:
	frappe.only_for("System Manager")
	checks = []
	for company in frappe.get_all("Company", pluck="name"):
		debit, credit = frappe.db.sql("""select ifnull(sum(debit), 0), ifnull(sum(credit), 0) from `tabGL Entry`
			where company = %s and is_cancelled = 0""", company)[0]
		checks.append({"check": f"trial balance {company}", "ok": abs(flt(debit) - flt(credit)) < 0.01,
			"debit": flt(debit), "credit": flt(credit)})
	from automotive_core.governance.audit import verify_chain

	chain = verify_chain()
	checks.append({"check": "audit hash chain", "ok": bool(chain["ok"]), "detail": chain})
	counts = {doctype: frappe.db.count(doctype) for doctype in COUNTED if frappe.db.table_exists(doctype)}
	checks.append({"check": "record counts", "ok": counts.get("Company", 0) > 0, "counts": counts})
	last = frappe.db.sql("select max(posting_date) from `tabGL Entry`")[0][0]
	checks.append({"check": "latest posting", "ok": True, "date": str(last) if last else None})
	return json.dumps({"ok": all(c["ok"] for c in checks), "checks": checks}, default=str)
