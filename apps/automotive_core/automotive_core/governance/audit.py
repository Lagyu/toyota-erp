"""Append-only audit log sealed into a SHA-256 hash chain (ERP-07, ERP-09, ERP-10).

Events are inserted in the business transaction (so they exist exactly when the
change commits) without a hash. A single sealer (MariaDB named lock) later assigns
``chain_index`` in sealing order and ``event_hash = sha256(previous_hash +
canonical(event))``. Sealed rows are protected by DB triggers (setup/db_guards.py);
``verify_chain`` recomputes the chain and reports the first break.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import now_datetime

from automotive_core.observability.context import get_correlation_id
from automotive_core.utils import canonical_json, commit, hook_set, sha256_hex

GENESIS = "0" * 64
SEALED_FIELDS = (
	"name",
	"event_type",
	"reference_doctype",
	"reference_name",
	"company",
	"actor",
	"on_behalf_of",
	"event_time",
	"correlation_id",
	"payload",
)
SUMMARY_FIELDS = (
	"company",
	"posting_date",
	"transaction_date",
	"party_type",
	"party",
	"supplier",
	"customer",
	"base_grand_total",
	"grand_total",
	"paid_amount",
	"total_debit",
	"currency",
	"status",
	"amended_from",
	"automotive_plant",
)


def record(
	event_type: str,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	payload: dict[str, Any] | None = None,
	company: str | None = None,
	actor: str | None = None,
	on_behalf_of: str | None = None,
) -> str:
	"""Insert one audit event in the current transaction and return its name."""
	doc = frappe.get_doc(
		{
			"doctype": "Audit Event",
			"event_type": event_type,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"company": company,
			"actor": actor or frappe.session.user,
			"on_behalf_of": on_behalf_of,
			"event_time": now_datetime(),
			"correlation_id": get_correlation_id(),
			"payload": canonical_json(payload or {}),
		}
	)
	doc.flags.ignore_permissions = True
	doc.flags.automotive_audit_insert = True
	doc.insert()
	if not frappe.flags.in_test:
		frappe.enqueue(
			"automotive_core.governance.audit.seal_pending",
			queue="short",
			job_id="automotive-audit-seal",
			deduplicate=True,
			enqueue_after_commit=True,
		)
	return doc.name


def document_summary(doc) -> dict[str, Any]:
	"""Minimal, non-sensitive snapshot of a document for audit payloads."""
	summary: dict[str, Any] = {"docstatus": doc.docstatus}
	for field in SUMMARY_FIELDS:
		if doc.meta.has_field(field) and doc.get(field) not in (None, ""):
			summary[field] = doc.get(field)
	if doc.flags.get("automotive_approval_request"):
		summary["approval_request"] = doc.flags.automotive_approval_request
	return summary


def on_submit(doc, method=None) -> None:
	if doc.doctype in hook_set("automotive_audited_doctypes"):
		event = "document.amended_and_submitted" if doc.get("amended_from") else "document.submitted"
		record(event, doc.doctype, doc.name, document_summary(doc), company=doc.get("company"))


def on_cancel(doc, method=None) -> None:
	if doc.doctype in hook_set("automotive_audited_doctypes") or doc.flags.get("cancel_reason"):
		payload = document_summary(doc)
		payload["reason"] = doc.flags.get("cancel_reason") or frappe.flags.get("automotive_cancel_reason")
		record("document.cancelled", doc.doctype, doc.name, payload, company=doc.get("company"))


def seal_pending(limit: int = 5000) -> int:
	"""Seal unsealed events in insertion order. Returns the number sealed."""
	if not frappe.db.get_single_value("Automotive Settings", "audit_chain_enabled"):
		return 0
	if not frappe.db.sql("SELECT GET_LOCK('automotive_audit_seal', 0)")[0][0]:
		return 0  # another sealer is running
	sealed = 0
	try:
		last = frappe.db.sql(
			"""select chain_index, event_hash from `tabAudit Event`
			where chain_index > 0 order by chain_index desc limit 1"""
		)
		index, head = (int(last[0][0]), last[0][1]) if last else (0, GENESIS)
		rows = frappe.db.sql(
			f"""select {", ".join(f"`{f}`" for f in SEALED_FIELDS)} from `tabAudit Event`
			where ifnull(chain_index, 0) = 0 order by name asc limit %s""",
			limit,
			as_dict=True,
		)
		now = now_datetime()
		for row in rows:
			index += 1
			event_hash = _hash(head, row)
			frappe.db.sql(
				"""update `tabAudit Event` set chain_index=%s, previous_hash=%s, event_hash=%s, sealed_at=%s
				where name=%s and ifnull(chain_index, 0) = 0""",
				(index, head, event_hash, now, row["name"]),
			)
			head = event_hash
			sealed += 1
		if sealed:
			frappe.db.set_single_value("Automotive Settings", "audit_chain_head", head)
		commit()
	finally:
		frappe.db.sql("SELECT RELEASE_LOCK('automotive_audit_seal')")
	return sealed


def _hash(previous_hash: str, row: dict[str, Any]) -> str:
	material = {field: row.get(field) for field in SEALED_FIELDS}
	material["name"] = str(material["name"])
	return sha256_hex(previous_hash + canonical_json(material))


def verify_chain(batch: int = 5000) -> dict[str, Any]:
	"""Recompute the whole chain. Returns {"ok", "checked", "first_break", "unsealed"}."""
	head, checked, last_index = GENESIS, 0, 0
	while True:
		rows = frappe.db.sql(
			f"""select chain_index, previous_hash, event_hash, {", ".join(f"`{f}`" for f in SEALED_FIELDS)}
			from `tabAudit Event` where chain_index > %s order by chain_index asc limit %s""",
			(last_index, batch),
			as_dict=True,
		)
		if not rows:
			break
		for row in rows:
			expected_index = last_index + 1
			if int(row["chain_index"]) != expected_index:
				return _result(False, checked, f"missing chain index {expected_index} (found {row['chain_index']})")
			if row["previous_hash"] != head or _hash(head, row) != row["event_hash"]:
				return _result(False, checked, f"hash mismatch at chain index {row['chain_index']} (event {row['name']})")
			head = row["event_hash"]
			last_index = expected_index
			checked += 1
	return _result(True, checked, None)


def _result(ok: bool, checked: int, first_break: str | None) -> dict[str, Any]:
	unsealed = frappe.db.sql("select count(*) from `tabAudit Event` where ifnull(chain_index, 0) = 0")[0][0]
	return {"ok": ok, "checked": checked, "first_break": first_break, "unsealed": unsealed}


def verify_chain_job() -> None:
	seal_pending()
	result = verify_chain()
	frappe.db.set_single_value(
		"Automotive Settings",
		{"audit_chain_verified_at": str(now_datetime()), "audit_chain_last_result": json.dumps(result)},
	)
	if not result["ok"]:
		frappe.log_error(title="Audit chain verification failed", message=json.dumps(result, indent=1))
	commit()
