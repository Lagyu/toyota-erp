"""Idempotent inbound message processing: exactly-once business effect over
at-least-once delivery (ERP-11, journey J-10).

1. ``receive`` (HTTPS) / ``accept`` (file drop, internal callers, tests) authenticate the
   partner, validate the envelope and store the message once per (partner,
   message_id) — a DB unique key backs the check. A redelivery with the same payload
   returns the original outcome; the same id with a different payload is refused.
2. ``process`` locks the message row, runs the registered handler *as the partner's
   service user* and marks the message Processed in the same transaction as the
   business documents, so a crash leaves either both or neither.
3. Failures are retried with exponential backoff, then dead-lettered; ``replay``
   re-queues a dead letter after the cause is fixed.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, cint, now_datetime

from automotive_core.governance import audit
from automotive_core.integration_hub import signing
from automotive_core.integration_hub.contracts import InboundEnvelope
from automotive_core.observability.context import get_correlation_id, log, set_correlation_id
from automotive_core.utils import canonical_json, commit, hook_dict, sha256_hex


class InboundRejected(Exception):
	"""Raised by handlers for messages that can never succeed (bad reference, wrong partner)."""


class PayloadConflictError(frappe.ValidationError):
	http_status_code = 409


def _settings():
	return frappe.get_cached_doc("Automotive Settings")


def backoff_seconds(attempt: int, base: int) -> int:
	return min(base * (2 ** max(attempt - 1, 0)), 3600)


@contextlib.contextmanager
def as_user(user: str | None):
	previous = frappe.session.user
	frappe.set_user(user or "Administrator")
	try:
		yield
	finally:
		frappe.set_user(previous)


def _allowed_types(partner) -> set[str]:
	return {t.strip() for t in (partner.allowed_message_types or "").splitlines() if t.strip()}


# ---------------------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------------------


def accept(partner_code: str, raw_body: bytes | str, source: str = "Internal") -> dict[str, Any]:
	"""Store a message once and queue it. Returns the AcceptedResponse dict."""
	if isinstance(raw_body, str):
		raw_body = raw_body.encode()
	partner = frappe.get_doc("Integration Partner", partner_code)
	if not partner.is_active:
		raise frappe.PermissionError(_("Integration partner {0} is inactive.").format(partner_code))
	envelope = _parse(partner, raw_body)
	if envelope.message_type not in _allowed_types(partner):
		raise frappe.PermissionError(
			_("Partner {0} may not send {1} messages.").format(partner_code, envelope.message_type)
		)
	payload_hash = sha256_hex(raw_body)
	existing = _existing(partner.name, envelope.message_id)
	if existing:
		return _duplicate_response(existing, payload_hash)
	msg = frappe.get_doc(
		{
			"doctype": "Integration Inbound Message",
			"partner": partner.name,
			"message_id": envelope.message_id,
			"message_type": envelope.message_type,
			"schema_version": envelope.schema_version,
			"source": source,
			"status": "Received",
			"received_at": now_datetime(),
			"correlation_id": get_correlation_id(),
			"payload": canonical_json(envelope.model_dump(mode="json")),
			"payload_sha256": payload_hash,
		}
	)
	msg.flags.ignore_permissions = True
	try:
		frappe.db.savepoint("inbound_accept")
		msg.insert()
	except frappe.DuplicateEntryError:
		frappe.db.rollback(save_point="inbound_accept")
		return _duplicate_response(_existing(partner.name, envelope.message_id), payload_hash)
	log("integration.inbound.accepted", partner=partner.name, message_id=envelope.message_id,
		message_type=envelope.message_type, inbound=msg.name)
	_enqueue(msg.name)
	return {"status": "accepted", "inbound_id": msg.name, "processing_status": msg.status,
		"result_doctype": None, "result_name": None}


def _parse(partner, raw_body: bytes) -> InboundEnvelope:
	from pydantic import ValidationError

	try:
		if partner.message_format == "Hinode EDI":
			from automotive_core.integration_hub.adapters.edi import decode

			data = decode(raw_body.decode())
		else:
			data = json.loads(raw_body)
		return InboundEnvelope.model_validate(data)
	except (ValueError, ValidationError) as exc:
		raise frappe.ValidationError(_("Invalid message: {0}").format(str(exc)[:500])) from exc


def _existing(partner: str, message_id: str):
	return frappe.db.get_value(
		"Integration Inbound Message",
		{"partner": partner, "message_id": message_id},
		["name", "payload_sha256", "status", "result_doctype", "result_name"],
		as_dict=True,
	)


def _duplicate_response(existing, payload_hash: str) -> dict[str, Any]:
	if existing.payload_sha256 != payload_hash:
		raise PayloadConflictError(
			_("Message id already used with a different payload (inbound {0}).").format(existing.name)
		)
	return {"status": "duplicate", "inbound_id": existing.name, "processing_status": existing.status,
		"result_doctype": existing.result_doctype, "result_name": existing.result_name}


def _enqueue(name: str) -> None:
	if frappe.flags.in_test or frappe.flags.automotive_inline_integration:
		frappe.flags.automotive_pending_inbound = [*(frappe.flags.automotive_pending_inbound or []), name]
		return
	frappe.enqueue(
		"automotive_core.integration_hub.inbound.process",
		queue="short",
		job_id=f"automotive-inbound-{name}",
		deduplicate=True,
		enqueue_after_commit=True,
		name=name,
		automotive_correlation_id=get_correlation_id(),
	)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receive() -> dict[str, Any]:
	"""HTTPS endpoint: POST /api/method/automotive_core.integration_hub.inbound.receive

	Headers: X-Hinode-Partner, X-Hinode-Signature, optional X-Correlation-ID.
	"""
	request = frappe.request
	partner_code = request.headers.get(signing.PARTNER_HEADER, "")
	body = request.get_data(cache=True) or b""
	if not partner_code or not frappe.db.exists("Integration Partner", partner_code):
		raise frappe.AuthenticationError(_("Unknown partner."))
	partner = frappe.get_doc("Integration Partner", partner_code)
	_rate_limit(partner.name)
	secret = partner.get_password("inbound_secret", raise_exception=False)
	if not secret:
		raise frappe.AuthenticationError(_("Partner has no inbound credentials configured."))
	tolerance = cint(_settings().webhook_tolerance_seconds) or 300
	try:
		sig = signing.verify(request.headers.get(signing.HEADER), body, secret, tolerance)
	except signing.SignatureError as exc:
		log("integration.inbound.signature_rejected", level="warning", partner=partner.name, reason=str(exc))
		raise frappe.AuthenticationError(_("Signature rejected: {0}").format(exc)) from exc
	_remember_nonce(partner.name, sig.nonce)
	with as_user(partner.service_user):
		response = accept(partner.name, body, source="HTTPS")
	frappe.local.response.http_status_code = 202 if response["status"] == "accepted" else 200
	return response


def _rate_limit(partner: str) -> None:
	limit = cint(_settings().inbound_rate_limit_per_minute) or 120
	key = f"automotive:inbound-rate:{partner}:{now_datetime().strftime('%Y%m%d%H%M')}"
	count = frappe.cache.incrby(key, 1)
	if count == 1:
		frappe.cache.expire(key, 120)
	if count > limit:
		frappe.local.response.http_status_code = 429
		raise frappe.TooManyRequestsError(_("Rate limit exceeded for partner {0}.").format(partner))


def _remember_nonce(partner: str, nonce: str) -> None:
	key = sha256_hex(f"{partner}:{nonce}")
	doc = frappe.get_doc({"doctype": "Integration Nonce", "nonce_key": key, "partner": partner, "seen_at": now_datetime()})
	doc.flags.ignore_permissions = True
	try:
		frappe.db.savepoint("inbound_nonce")
		doc.insert()
	except frappe.DuplicateEntryError:
		frappe.db.rollback(save_point="inbound_nonce")
		raise frappe.AuthenticationError(_("Replayed request (nonce already used).")) from None


# ---------------------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------------------


def process(name: str, automotive_correlation_id: str | None = None) -> str:
	msg = frappe.get_doc("Integration Inbound Message", name, for_update=True)
	if automotive_correlation_id or msg.correlation_id:
		set_correlation_id(automotive_correlation_id or msg.correlation_id)
	if msg.status in ("Processed", "Dead Letter", "Rejected"):
		return msg.status
	handlers = hook_dict("automotive_inbound_handlers")
	handler_path = handlers.get(msg.message_type)
	partner = frappe.get_doc("Integration Partner", msg.partner)
	msg.attempts = cint(msg.attempts) + 1
	if not handler_path:
		return _finish(msg, "Rejected", error=f"no handler registered for {msg.message_type}")
	envelope = json.loads(msg.payload)
	frappe.db.savepoint("inbound_process")
	try:
		with as_user(partner.service_user):
			result = frappe.get_attr(handler_path)(message=msg, partner=partner, data=envelope["data"])
	except InboundRejected as exc:
		frappe.db.rollback(save_point="inbound_process")
		return _finish(msg, "Rejected", error=str(exc))
	except Exception as exc:
		frappe.db.rollback(save_point="inbound_process")
		error = f"{type(exc).__name__}: {exc}"
		max_attempts = cint(_settings().inbound_max_attempts) or 6
		if msg.attempts >= max_attempts:
			return _finish(msg, "Dead Letter", error=error)
		msg.next_attempt_at = add_to_date(
			now_datetime(), seconds=backoff_seconds(msg.attempts, cint(_settings().outbox_base_backoff_seconds) or 30)
		)
		return _finish(msg, "Failed", error=error)
	result_doctype, result_name = result if result else (None, None)
	msg.result_doctype = result_doctype
	msg.result_name = result_name
	msg.processed_at = now_datetime()
	return _finish(msg, "Processed")


def _finish(msg, status: str, error: str | None = None) -> str:
	msg.status = status
	msg.last_error = (error or "")[:2000] or None
	msg.flags.ignore_permissions = True
	msg.save()
	if status in ("Dead Letter", "Rejected"):
		audit.record(f"integration.inbound.{status.lower().replace(' ', '_')}", msg.doctype, msg.name,
			{"partner": msg.partner, "message_id": msg.message_id, "error": msg.last_error})
	log("integration.inbound." + status.lower().replace(" ", "_"), inbound=msg.name, partner=msg.partner,
		message_id=msg.message_id, attempts=msg.attempts, error=msg.last_error)
	commit()
	return status


def run_pending_inline() -> list[str]:
	"""Process messages queued while frappe.flags.in_test / automotive_inline_integration is set."""
	pending, frappe.flags.automotive_pending_inbound = frappe.flags.automotive_pending_inbound or [], []
	return [process(name) for name in pending]


def retry_due() -> None:
	now = now_datetime()
	due = frappe.get_all(
		"Integration Inbound Message",
		filters={"status": "Failed", "next_attempt_at": ("<=", now)},
		pluck="name",
		limit=200,
	)
	stale = frappe.get_all(
		"Integration Inbound Message",
		filters={"status": ("in", ("Received", "Processing")), "modified": ("<", add_to_date(now, minutes=-5))},
		pluck="name",
		limit=200,
	)
	for name in dict.fromkeys(due + stale):
		_enqueue(name)


@frappe.whitelist(methods=["POST"])
def replay(name: str) -> str:
	frappe.only_for(("IT Operator", "System Manager"))
	msg = frappe.get_doc("Integration Inbound Message", name)
	if msg.status not in ("Dead Letter", "Failed", "Rejected"):
		frappe.throw(_("Only failed, rejected or dead-lettered messages can be replayed."))
	msg.status = "Received"
	msg.next_attempt_at = None
	msg.flags.ignore_permissions = True
	msg.save()
	audit.record("integration.inbound.replayed", msg.doctype, msg.name, {"previous_attempts": msg.attempts})
	_enqueue(msg.name)
	return msg.status


@frappe.whitelist()
def reconciliation() -> dict[str, Any]:
	"""Counts by status plus integrity checks used by the J-10 acceptance test."""
	frappe.only_for(("IT Operator", "System Manager", "Auditor"))
	by_status = dict(
		frappe.db.sql("select status, count(*) from `tabIntegration Inbound Message` group by status")
	)
	missing = frappe.db.sql(
		"""select m.name, m.result_doctype, m.result_name from `tabIntegration Inbound Message` m
		where m.status = 'Processed' and m.result_name is not null""",
		as_dict=True,
	)
	orphans = [m.name for m in missing if m.result_doctype and not frappe.db.exists(m.result_doctype, m.result_name)]
	shared = frappe.db.sql(
		"""select result_doctype, result_name, count(*) c from `tabIntegration Inbound Message`
		where status='Processed' and result_name is not null group by result_doctype, result_name having c > 1"""
	)
	return {"by_status": by_status, "processed_without_result_document": orphans,
		"results_shared_by_several_messages": [list(r) for r in shared], "ok": not orphans and not shared}
