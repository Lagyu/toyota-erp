"""Idempotency keys for state-changing API methods (ERP-11, §6.3).

    @frappe.whitelist(methods=["POST"])
    @idempotent("dealer.create_order")
    def create_order(...): ...

The client sends ``Idempotency-Key``. The first call inserts an In Progress record
(unique scope key) *in the same transaction* as its business effect and stores the
response; a concurrent duplicate blocks on the unique index until the first commits
and then replays the stored response, read in a new transaction (its own snapshot
predates the first's commit). Reusing a key for a different request is a 422.
Records expire after 7 days (integration_hub.maintenance.prune).
"""

from __future__ import annotations

import functools
import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_days, now_datetime

from automotive_core.utils import canonical_json, sha256_hex

HEADER = "Idempotency-Key"
TTL_DAYS = 7


class IdempotencyConflictError(frappe.ValidationError):
	http_status_code = 422


class IdempotencyInProgressError(frappe.ValidationError):
	http_status_code = 409


def current_key(kwargs: dict[str, Any]) -> str | None:
	key = kwargs.pop("idempotency_key", None)
	if not key and getattr(frappe.local, "request", None):
		key = frappe.get_request_header(HEADER)
	return (key or "").strip() or None


def idempotent(endpoint: str, required: bool = True):
	def decorator(fn):
		@functools.wraps(fn)
		def wrapper(*args, **kwargs):
			key = current_key(kwargs)
			if not key:
				if required:
					frappe.throw(_("Header {0} is required for this request.").format(HEADER))
				return fn(*args, **kwargs)
			if len(key) > 120:
				frappe.throw(_("{0} is too long.").format(HEADER))
			user = frappe.session.user
			scope = sha256_hex(f"{endpoint}\n{user}\n{key}")
			request_hash = sha256_hex(canonical_json({"args": list(args), "kwargs": kwargs}))
			existing = _load(scope)
			if existing:
				return _replay(existing, request_hash)
			record = frappe.get_doc(
				{
					"doctype": "Idempotency Record",
					"scope_key": scope,
					"endpoint": endpoint,
					"idempotency_key": key,
					"user": user,
					"status": "In Progress",
					"request_sha256": request_hash,
					"expires_at": add_days(now_datetime(), TTL_DAYS),
				}
			)
			record.flags.ignore_permissions = True
			try:
				frappe.db.savepoint("idempotency")
				record.insert()
			except (frappe.DuplicateEntryError, frappe.UniqueValidationError, frappe.QueryDeadlockError):
				# the scope key is a unique field, not the name: Frappe reports UniqueValidationError
				# (or, under MariaDB's snapshot isolation, a deadlock). This insert waited for the
				# first request to commit; nothing is written yet, so start over in a new
				# transaction, whose snapshot sees the first one's record.
				frappe.db.rollback()
				frappe.clear_messages()  # the duplicate entry message is not this request's answer
				existing = _load(scope)
				if not existing:
					raise IdempotencyInProgressError(_("A request with this Idempotency-Key is still being processed."))
				return _replay(existing, request_hash)
			result = fn(*args, **kwargs)
			record.db_set({"status": "Completed", "response": json.dumps(result, default=str), "http_status": 200},
				update_modified=False)
			return result

		wrapper.automotive_idempotent_endpoint = endpoint
		return wrapper

	return decorator


def _load(scope: str):
	return frappe.db.get_value(
		"Idempotency Record", {"scope_key": scope}, ["name", "status", "request_sha256", "response"], as_dict=True
	)


def _replay(existing, request_hash: str):
	if existing.request_sha256 != request_hash:
		raise IdempotencyConflictError(_("This Idempotency-Key was already used for a different request."))
	if existing.status != "Completed":
		raise IdempotencyInProgressError(_("A request with this Idempotency-Key is still being processed."))
	frappe.local.response["idempotent_replay"] = True
	return json.loads(existing.response) if existing.response else None
