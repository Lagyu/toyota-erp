"""Notifications with preferences and auditable dispatch results (ERP-11.5)."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from automotive_core.governance import notifications
from automotive_core.governance.notifications import notify, set_my_preference, sync_email_results
from automotive_core.tests.utils import as_user, make_user

USER = "notify.planner@hinode.example"
USER_JA = "notify.planner.ja@hinode.example"


class TestNotifications(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		notifications.ensure_templates()
		if not frappe.db.exists("Email Account", {"default_outgoing": 1}):
			frappe.get_doc({"doctype": "Email Account", "email_account_name": "Test Outgoing", "email_id":
				"notifications@hinode.example", "enable_outgoing": 1, "default_outgoing": 1, "smtp_server": "localhost",
				"smtp_port": 25, "no_smtp_authentication": 1, "awaiting_password": 0}).insert(ignore_permissions=True)
		make_user(USER, ["Production Planner"])
		make_user(USER_JA, ["Production Planner"])
		frappe.db.set_value("User", USER_JA, "language", "ja")
		cls.todo = frappe.get_doc({"doctype": "ToDo", "description": "notification test"}).insert(ignore_permissions=True)

	def _rows(self, names: list[str]) -> dict[tuple[str, str], dict]:
		rows = frappe.get_all("Notification Dispatch", filters={"name": ("in", names)},
			fields=["recipient", "channel", "status", "email_queue", "detail", "subject"])
		return {(r.recipient, r.channel): r for r in rows}

	def test_desk_and_email_by_default_each_with_its_dispatch_row(self):
		rows = self._rows(notify("integration.dead_letter", [USER], "ToDo", self.todo.name))
		self.assertEqual(rows[(USER, "In-App")].status, "Delivered")
		email = rows[(USER, "Email")]
		self.assertEqual(email.status, "Queued")
		self.assertEqual(frappe.db.get_value("Email Queue", email.email_queue, "reference_name"), self.todo.name)
		self.assertTrue(frappe.db.exists("Notification Log", {"for_user": USER, "document_name": self.todo.name}))

	def test_preferences_turn_channels_off_but_not_mandatory_desk_alerts(self):
		with as_user(USER):
			set_my_preference("integration.dead_letter", in_app=0, email=0)
			choice = set_my_preference("approval.requested", in_app=0, email=1)
		self.assertEqual(choice["in_app"], 1)  # mandatory: the desk alert stays on
		rows = self._rows(notify("integration.dead_letter", [USER], "ToDo", self.todo.name))
		self.assertEqual({k[1]: r.status for k, r in rows.items()}, {"In-App": "Suppressed", "Email": "Suppressed"})
		self.assertIn("preferences", rows[(USER, "Email")].detail)

	def test_email_results_are_recorded_from_the_queue(self):
		sent, failed = (self._rows(notify("integration.dead_letter", [user], "ToDo", self.todo.name))[(user, "Email")]
			for user in (USER, USER_JA))
		frappe.db.set_value("Email Queue", sent.email_queue, "status", "Sent")
		frappe.db.set_value("Email Queue", failed.email_queue, {"status": "Error", "error": "550 mailbox unavailable"})
		self.assertGreaterEqual(sync_email_results(), 2)
		self.assertEqual(frappe.db.get_value("Notification Dispatch", {"email_queue": sent.email_queue}, "status"), "Sent")
		result = frappe.db.get_value("Notification Dispatch", {"email_queue": failed.email_queue}, ["status", "detail"])
		self.assertEqual(result[0], "Failed")
		self.assertIn("550", result[1])

	def test_a_missing_mail_account_fails_the_email_not_the_business_step(self):
		account = frappe.db.get_value("Email Account", {"default_outgoing": 1}, "name")
		frappe.db.set_value("Email Account", account, "default_outgoing", 0)
		frappe.clear_cache()

		def restore():
			frappe.db.set_value("Email Account", account, "default_outgoing", 1)
			frappe.clear_cache()

		self.addCleanup(restore)
		rows = self._rows(notify("integration.dead_letter", [USER], "ToDo", self.todo.name))
		self.assertEqual(rows[(USER, "In-App")].status, "Delivered")
		self.assertEqual(rows[(USER, "Email")].status, "Failed")
		self.assertIn("Email Account", rows[(USER, "Email")].detail)

	def test_each_recipient_reads_it_in_their_language(self):
		rows = self._rows(notify("integration.dead_letter", [USER, USER_JA], "ToDo", self.todo.name))
		self.assertIn("dead-lettered", rows[(USER, "In-App")].subject)
		self.assertIn("デッドレター", rows[(USER_JA, "In-App")].subject)
