"""Notifications (ERP-11.5): one entry point for what the apps tell people.

``notify(event, users, doctype, name)`` reaches each user as their Notification Preference
for the event says:
- in the desk: an Alert in Frappe's Notification Log; events marked mandatory always arrive
  there;
- by e-mail: rendered from the event's Email Template in the recipient's language, sent
  through Frappe's Email Queue.

Every recipient and channel gets a Notification Dispatch row: Delivered (desk), Queued, Sent
or Failed (e-mail, followed from the Email Queue by ``sync_email_results``), or Suppressed
with the reason. That row is the auditable dispatch result. Bulk loads (data generation,
imports) send nothing.

The Email Templates ("Automotive: <event>") are created from
``templates/emails/<event>.subject.html`` and ``<event>.html`` on migrate. Administrators may
edit them afterwards; a migrate does not overwrite them.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

import frappe
from frappe import _
from frappe.utils import get_url_to_form, now_datetime

TEMPLATES = Path(__file__).resolve().parent.parent / "templates" / "emails"


@dataclass(frozen=True)
class Event:
	label: str
	email: bool  # e-mail by default (a preference can turn it off)
	mandatory: bool = False  # always reaches the desk


EVENTS = {
	"approval.requested": Event("Approval requested", email=True, mandatory=True),
	"approval.decided": Event("Approval decided", email=True),
	"kanban.overdue": Event("Kanban signal overdue", email=False),
	"quality.hold_raised": Event("Quality hold raised", email=True),
	"integration.dead_letter": Event("Integration message dead-lettered", email=True),
}


def template_name(event: str) -> str:
	return f"Automotive: {EVENTS[event].label}"


def ensure_templates() -> None:
	"""after_migrate: create missing Email Templates from the files (edits are kept)."""
	for event in EVENTS:
		name = template_name(event)
		if frappe.db.exists("Email Template", name):
			continue
		stem = event.replace(".", "_")
		frappe.get_doc({"doctype": "Email Template", "name": name, "__newname": name, "use_html": 1,
			"subject": (TEMPLATES / f"{stem}.subject.html").read_text().strip(),
			"response_html": (TEMPLATES / f"{stem}.html").read_text()}).insert(ignore_permissions=True)


@contextlib.contextmanager
def _language(lang: str):
	previous = getattr(frappe.local, "lang", None)
	frappe.local.lang = lang
	try:
		yield
	finally:
		frappe.local.lang = previous


def _preference(user: str, event: str) -> tuple[bool, bool]:
	spec = EVENTS[event]
	row = frappe.db.get_value("Notification Preference Event", {"parent": user, "event": event},
		["in_app", "email"], as_dict=True)
	in_app, email = (bool(row.in_app), bool(row.email)) if row else (True, spec.email)
	return in_app or spec.mandatory, email


def _render(event: str, doctype: str | None, name: str | None, context: dict) -> tuple[str, str]:
	template = frappe.get_cached_doc("Email Template", template_name(event))
	args = {"doc": frappe.get_doc(doctype, name).as_dict() if doctype and name else {},
		"link": get_url_to_form(doctype, name) if doctype and name else "", **context}
	return frappe.render_template(template.subject, args).strip(), frappe.render_template(template.response_html, args)


def _dispatch(event, user, channel, status, doctype, name, subject, **values) -> str:
	doc = frappe.get_doc({"doctype": "Notification Dispatch", "event": event, "recipient": user, "channel": channel,
		"status": status, "reference_doctype": doctype, "reference_name": name, "subject": (subject or "")[:140],
		**values})
	doc.insert(ignore_permissions=True)
	return doc.name


def notify(event: str, users, doctype: str | None = None, name: str | None = None, **context) -> list[str]:
	"""Tell ``users`` about ``event`` on ``doctype`` ``name``; returns the dispatch rows.
	``context`` is passed to the event's templates (alongside ``doc`` and ``link``)."""
	if event not in EVENTS:
		frappe.throw(_("Unknown notification event {0}").format(event))
	if frappe.flags.in_import or frappe.flags.automotive_bulk:
		return []
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

	rows = []
	for user in sorted({u for u in users or [] if u and u not in ("Guest", "Administrator")}):
		in_app, email = _preference(user, event)
		lang = frappe.db.get_value("User", user, "language") or frappe.db.get_default("lang") or "en"
		with _language(lang):
			subject, body = _render(event, doctype, name, context)
			off = _("Turned off in the recipient's notification preferences")
		if in_app:
			enqueue_create_notification([user], {"type": "Alert", "document_type": doctype, "document_name": name,
				"subject": subject, "from_user": frappe.session.user})
			rows.append(_dispatch(event, user, "In-App", "Delivered", doctype, name, subject))
		else:
			rows.append(_dispatch(event, user, "In-App", "Suppressed", doctype, name, subject, detail=off))
		if not email:
			rows.append(_dispatch(event, user, "Email", "Suppressed", doctype, name, subject, detail=off))
			continue
		address = frappe.db.get_value("User", user, "email")
		try:
			queue = frappe.sendmail(recipients=[address], subject=subject, message=body, reference_doctype=doctype,
				reference_name=name, delayed=True) if address else None
		except frappe.OutgoingEmailError as exc:
			# no outgoing account (a new site): the business step goes on, the failure is on record
			frappe.clear_last_message()
			rows.append(_dispatch(event, user, "Email", "Failed", doctype, name, subject, detail=str(exc)[:1000]))
			continue
		if queue:
			rows.append(_dispatch(event, user, "Email", "Queued", doctype, name, subject, email_queue=queue.name))
		else:
			rows.append(_dispatch(event, user, "Email", "Suppressed", doctype, name, subject,
				detail=_("No e-mail address, or the address unsubscribed")))
	return rows


def users_with_role(role: str, plant: str | None = None) -> list[str]:
	"""Enabled desk users holding ``role`` (those assigned to ``plant`` when there are any)."""
	users = set(frappe.get_all("Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent"))
	users &= set(frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, pluck="name"))
	if plant:
		assigned = users & set(frappe.get_all("Plant Assignment", filters={"plant": plant, "status": "Active"},
			pluck="user"))
		users = assigned or users
	return sorted(users - {"Administrator", "Guest"})[:50]


def dead_letter(doctype: str, name: str) -> None:
	"""An integration message the hub gave up on: the IT operators hear about it."""
	notify("integration.dead_letter", users_with_role("IT Operator"), doctype, name)


EMAIL_RESULT = {"Sent": "Sent", "Partially Sent": "Sent", "Error": "Failed"}


def sync_email_results() -> int:
	"""Scheduled: record what the Email Queue did with queued notification e-mails."""
	rows = frappe.db.sql("""select d.name, q.status, q.error, q.modified from `tabNotification Dispatch` d
		join `tabEmail Queue` q on q.name = d.email_queue where d.status = 'Queued' and q.status in %s""",
		(tuple(EMAIL_RESULT),), as_dict=True)
	for row in rows:
		values = {"status": EMAIL_RESULT[row.status], "sent_at": row.modified if row.status != "Error" else None}
		if row.status == "Error":
			values["detail"] = (row.error or "")[-1000:]
		frappe.db.set_value("Notification Dispatch", row.name, values, update_modified=True)
	return len(rows)


@frappe.whitelist(methods=["POST"])
def set_my_preference(event: str, in_app: int = 1, email: int = 1) -> dict:
	"""The signed-in user's own channel choice for one event."""
	if event not in EVENTS:
		frappe.throw(_("Unknown notification event {0}").format(event))
	user = frappe.session.user
	doc = frappe.get_doc("Notification Preference", user) if frappe.db.exists("Notification Preference", user) else \
		frappe.get_doc({"doctype": "Notification Preference", "user": user})
	row = next((r for r in doc.events if r.event == event), None) or doc.append("events", {"event": event})
	row.in_app = 1 if EVENTS[event].mandatory else int(in_app)
	row.email = int(email)
	doc.save(ignore_permissions=True)
	return {"event": event, "in_app": row.in_app, "email": row.email, "at": str(now_datetime())}
