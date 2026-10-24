"""Request/job correlation IDs and structured logging (ERP-11, §6.2).

Every HTTP request gets a correlation id (incoming ``X-Correlation-ID`` if it is a
safe token, else a new one), echoed in the response. Background jobs inherit the id
of the request that enqueued them through the ``automotive_correlation_id`` job
kwarg, and integration messages store it, so one id follows a business event across
web, queue and partner traffic.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

import frappe

HEADER = "X-Correlation-ID"
_SAFE = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


def new_id() -> str:
	return uuid.uuid4().hex


def get_correlation_id() -> str:
	cid = getattr(frappe.local, "automotive_correlation_id", None)
	if not cid:
		cid = new_id()
		frappe.local.automotive_correlation_id = cid
	return cid


def set_correlation_id(cid: str | None) -> str:
	frappe.local.automotive_correlation_id = cid if cid and _SAFE.match(cid) else new_id()
	return frappe.local.automotive_correlation_id


def before_request() -> None:
	incoming = frappe.get_request_header(HEADER)
	set_correlation_id(incoming)


def after_request(response=None, request=None) -> None:
	if response is not None:
		response.headers[HEADER] = get_correlation_id()


def before_job(method: str | None = None, kwargs: dict | None = None, transaction_type: str | None = None) -> None:
	cid = (kwargs or {}).get("automotive_correlation_id") if isinstance(kwargs, dict) else None
	set_correlation_id(cid)


def log(event: str, level: str = "info", **fields: Any) -> None:
	"""One JSON line in logs/automotive.log with the correlation id and site."""
	record = {
		"event": event,
		"correlation_id": get_correlation_id(),
		"site": getattr(frappe.local, "site", None),
		"user": getattr(getattr(frappe.local, "session", None), "user", None),
		**fields,
	}
	logger = frappe.logger("automotive", allow_site=True, file_count=10, max_size=50_000_000)
	if logger.level > logging.INFO:
		# Frappe creates loggers at ERROR outside the development server; these lines are the
		# operational record (integration traffic, failures) and are wanted in production
		logger.setLevel(logging.INFO)
	getattr(logger, level)(json.dumps(record, default=str, sort_keys=True))
