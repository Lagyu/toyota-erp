"""Transactional outbox with signed delivery, retries and dead-lettering (ERP-11).

``publish`` writes one Integration Outbound Message per subscribed partner in the
*same transaction* as the business change, keyed by an idempotency key, so an event
is neither lost (the change committed) nor duplicated (the key is unique). The
dispatcher claims due messages with ``FOR UPDATE SKIP LOCKED`` (safe with several
workers), delivers through the partner's adapter and records every attempt.
"""

from __future__ import annotations

import time
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from automotive_core.governance import audit
from automotive_core.integration_hub.adapters import get_adapter
from automotive_core.integration_hub.adapters.base import DeliveryResult
from automotive_core.integration_hub.inbound import backoff_seconds
from automotive_core.observability.context import get_correlation_id, log
from automotive_core.utils import canonical_json, commit, sha256_hex


def _settings():
	return frappe.get_cached_doc("Automotive Settings")


def subscribers(event_type: str, party_type: str | None = None, party: str | None = None) -> list[str]:
	rows = frappe.get_all(
		"Integration Partner",
		filters={"is_active": 1},
		fields=["name", "subscribed_events", "party_type", "party"],
	)
	out = []
	for row in rows:
		events = {e.strip() for e in (row.subscribed_events or "").splitlines() if e.strip()}
		if event_type not in events:
			continue
		if party and (row.party_type, row.party) != (party_type, party):
			continue
		out.append(row.name)
	return out


def publish(
	event_type: str,
	data: dict[str, Any],
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	partners: list[str] | None = None,
	party_type: str | None = None,
	party: str | None = None,
	idempotency_key: str | None = None,
) -> list[str]:
	"""Queue an event for every subscribed partner (or ``partners``). Returns message names."""
	targets = partners if partners is not None else subscribers(event_type, party_type, party)
	created = []
	for partner in targets:
		key = idempotency_key or f"{event_type}:{reference_doctype}:{reference_name}"
		key = f"{key}:{partner}"
		if frappe.db.exists("Integration Outbound Message", {"idempotency_key": key}):
			continue
		envelope = {
			"event_id": sha256_hex(key)[:32],
			"event_type": event_type,
			"schema_version": "v1",
			"occurred_at": now_datetime(),
			"correlation_id": get_correlation_id(),
			"data": data,
		}
		payload = canonical_json(envelope)
		msg = frappe.get_doc(
			{
				"doctype": "Integration Outbound Message",
				"partner": partner,
				"event_type": event_type,
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"idempotency_key": key,
				"correlation_id": get_correlation_id(),
				"status": "Pending",
				"next_attempt_at": now_datetime(),
				"payload": payload,
				"payload_sha256": sha256_hex(payload),
			}
		)
		msg.flags.ignore_permissions = True
		msg.insert()
		created.append(msg.name)
	if created and not frappe.flags.in_test:
		frappe.enqueue("automotive_core.integration_hub.outbox.dispatch_due", queue="short",
			job_id="automotive-outbox-dispatch", deduplicate=True, enqueue_after_commit=True)
	return created


def claim_due(limit: int = 50) -> list[str]:
	names = [
		row[0]
		for row in frappe.db.sql(
			"""select name from `tabIntegration Outbound Message`
			where status in ('Pending', 'Failed') and next_attempt_at <= %s
			order by next_attempt_at asc limit %s for update skip locked""",
			(now_datetime(), limit),
		)
	]
	if names:
		frappe.db.sql(
			"""update `tabIntegration Outbound Message` set status='Sending', modified=%s
			where name in %s""",
			(now_datetime(), tuple(names)),
		)
	commit()
	return names


def dispatch_due(limit: int = 50) -> int:
	_release_stuck()
	delivered = 0
	for name in claim_due(limit):
		if deliver(name) == "Delivered":
			delivered += 1
	return delivered


def _release_stuck() -> None:
	"""A worker that died mid-delivery leaves 'Sending'; make those due again after 10 minutes."""
	frappe.db.sql(
		"""update `tabIntegration Outbound Message` set status='Failed', next_attempt_at=%s
		where status='Sending' and modified < %s""",
		(now_datetime(), add_to_date(now_datetime(), minutes=-10)),
	)


def classify(result: DeliveryResult) -> str:
	if result.ok:
		return "Delivered"
	code = result.status_code or 0
	if code == 0 or code in (408, 409, 425, 429) or code >= 500:
		return "Retryable Failure"
	return "Permanent Failure"


def deliver(name: str) -> str:
	msg = frappe.get_doc("Integration Outbound Message", name)
	partner = frappe.get_doc("Integration Partner", msg.partner)
	adapter = get_adapter(partner)
	started = time.monotonic()
	try:
		result = adapter.send(partner, msg)
	except Exception as exc:  # adapter bugs must not lose the message
		result = DeliveryResult(ok=False, status_code=0, error=f"{type(exc).__name__}: {exc}")
	duration_ms = int((time.monotonic() - started) * 1000)
	outcome = classify(result)
	msg.attempts = cint(msg.attempts) + 1
	msg.append(
		"delivery_attempts",
		{
			"attempt_no": msg.attempts,
			"attempted_at": now_datetime(),
			"duration_ms": duration_ms,
			"response_code": result.status_code,
			"outcome": outcome,
			"error": (result.error or "")[:1000],
		},
	)
	msg.last_response_code = result.status_code
	msg.last_error = result.error
	max_attempts = cint(_settings().outbox_max_attempts) or 8
	if outcome == "Delivered":
		msg.status = "Delivered"
		msg.delivered_at = now_datetime()
	elif outcome == "Permanent Failure" or msg.attempts >= max_attempts:
		msg.status = "Dead Letter"
	else:
		msg.status = "Failed"
		msg.next_attempt_at = add_to_date(
			now_datetime(), seconds=backoff_seconds(msg.attempts, cint(_settings().outbox_base_backoff_seconds) or 30)
		)
	msg.flags.ignore_permissions = True
	msg.save()
	_update_partner_health(partner, outcome == "Delivered", result.error)
	if msg.status == "Dead Letter":
		audit.record("integration.outbound.dead_letter", msg.doctype, msg.name,
			{"partner": partner.name, "event_type": msg.event_type, "error": result.error})
	log("integration.outbound." + outcome.lower().replace(" ", "_"), outbound=msg.name, partner=partner.name,
		attempt=msg.attempts, status_code=result.status_code, duration_ms=duration_ms)
	commit()
	return msg.status


def _update_partner_health(partner, ok: bool, error: str | None) -> None:
	values: dict[str, Any] = {"last_success_at": now_datetime()} if ok else {"last_failure_at": now_datetime(),
		"last_error": (error or "")[:500]}
	frappe.db.set_value("Integration Partner", partner.name, values, update_modified=False)
	from automotive_core.integration_hub.partners import connection_status

	partner.reload()
	status = connection_status(partner)
	if status != partner.connection_status:
		frappe.db.set_value("Integration Partner", partner.name, "connection_status", status, update_modified=False)


@frappe.whitelist(methods=["POST"])
def retry(name: str) -> str:
	frappe.only_for(("IT Operator", "System Manager"))
	msg = frappe.get_doc("Integration Outbound Message", name)
	if msg.status not in ("Dead Letter", "Failed"):
		frappe.throw(_("Only failed or dead-lettered messages can be retried."))
	frappe.db.set_value("Integration Outbound Message", name, {"status": "Pending", "next_attempt_at": now_datetime()})
	audit.record("integration.outbound.retried", msg.doctype, msg.name, {"previous_attempts": msg.attempts})
	return "Pending"
