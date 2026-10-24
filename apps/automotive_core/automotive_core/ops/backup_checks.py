"""Integrity checks run on a freshly restored copy of a site (ops/backup/restore-drill.sh).

They prove a backup is usable, not just present: the ledgers balance, the audit hash
chain verifies end to end, the core record counts are there, and every stored file the
records point to was restored with them.
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import flt

COUNTED = ("Company", "User", "Item", "BOM", "Work Order", "Purchase Order", "Sales Invoice", "GL Entry",
	"Stock Ledger Entry", "Audit Event", "Vehicle Unit")


def verify_restored_site(output: str | None = None) -> dict:
	"""Returns the check results; with ``output`` (an absolute path) also writes them there
	as JSON, which the drill script reads instead of parsing ``bench execute`` output."""
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
	checks.append(_attachments())
	last = frappe.db.sql("select max(posting_date) from `tabGL Entry`")[0][0]
	checks.append({"check": "latest posting", "ok": True, "date": str(last) if last else None})
	result = {"ok": all(c["ok"] for c in checks), "checks": checks}
	if output:
		with open(output, "w") as handle:
			json.dump(result, handle, default=str)
	return result


def _attachments() -> dict:
	"""Every public and private file referenced by a File record exists on disk."""
	import os
	from urllib.parse import unquote

	total, missing = 0, []
	for url in frappe.get_all("File", filters={"is_folder": 0}, pluck="file_url", limit_page_length=0):
		url = unquote(url or "")
		if url.startswith("/private/files/"):
			path = frappe.get_site_path(url.lstrip("/"))
		elif url.startswith("/files/"):
			path = frappe.get_site_path("public", url.lstrip("/"))
		else:
			continue  # external links
		total += 1
		if not os.path.exists(path):
			missing.append(url)
	return {"check": "attachments restored", "ok": not missing, "files": total, "missing": len(missing),
		"examples": missing[:5]}
