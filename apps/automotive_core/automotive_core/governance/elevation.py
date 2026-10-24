"""System steps performed on behalf of a requester (ERP-10 accountability).

Some changes are *requested* by a user who may not perform them directly: a dealer's
validated change request rewrites the Sales Order and its stock reservations. The
request itself is authorised by the business rules (party ownership, order state);
the follow-up runs with system rights, and the audit trail records both the system
actor and the person it acted for.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import frappe

from automotive_core.governance import audit

SYSTEM_USER = "Administrator"


@contextlib.contextmanager
def on_behalf_of(
	purpose: str, reference_doctype: str | None = None, reference_name: str | None = None
) -> Iterator[str]:
	requester = frappe.session.user
	if requester == SYSTEM_USER:
		yield requester
		return
	audit.record(
		"system.on_behalf_of",
		reference_doctype,
		reference_name,
		{"purpose": purpose},
		actor=SYSTEM_USER,
		on_behalf_of=requester,
	)
	frappe.set_user(SYSTEM_USER)
	try:
		yield requester
	finally:
		frappe.set_user(requester)
