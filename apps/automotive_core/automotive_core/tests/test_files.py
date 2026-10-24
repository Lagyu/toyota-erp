"""Attachment controls (ERP-11, SEC: safe file handling): allowed types and sizes, content
that must match its extension, private attachments, downloads gated on the virus scan,
and retention."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime

from automotive_core.files import policy
from automotive_core.files.policy import AttachmentRejectedError
from automotive_core.tests.utils import as_user, make_user

READER = "files.reader@hinode.example"


def _pdf() -> bytes:
	"""A valid one-page PDF (Frappe parses uploaded PDFs: a sketch of one is refused)."""
	objects = [
		b"<< /Type /Catalog /Pages 2 0 R >>",
		b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
		b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
	]
	out, offsets = bytearray(b"%PDF-1.4\n"), []
	for number, body in enumerate(objects, start=1):
		offsets.append(len(out))
		out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
	xref = len(out)
	out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
	out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
	out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
	return bytes(out)


PDF = _pdf()


class TestAttachmentPolicy(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(READER, ["Production Planner"])
		cls.todo = frappe.get_doc(
			{"doctype": "ToDo", "description": "attachments", "allocated_to": READER}
		).insert(ignore_permissions=True)

	def setUp(self):
		settings = frappe.get_single("Automotive Settings")
		saved = {
			k: settings.get(k)
			for k in ("max_upload_mb", "virus_scan_mode", "default_retention_days", "clamd_host")
		}
		rules = [r.as_dict() for r in settings.attachment_rules]

		def restore():
			doc = frappe.get_single("Automotive Settings")
			doc.update(saved)
			doc.set(
				"attachment_rules",
				[
					{
						k: r[k]
						for k in (
							"reference_doctype",
							"allowed_extensions",
							"max_size_mb",
							"retention_days",
							"private_only",
						)
					}
					for r in rules
				],
			)
			doc.save(ignore_permissions=True)

		self.addCleanup(restore)

	def _settings(self, **values):
		doc = frappe.get_single("Automotive Settings")
		doc.update(values)
		doc.save(ignore_permissions=True)

	def attach(self, name, content, doctype="ToDo", docname=None, private=1):
		return frappe.get_doc(
			{
				"doctype": "File",
				"file_name": name,
				"content": content,
				"is_private": private,
				"attached_to_doctype": doctype,
				"attached_to_name": docname or self.todo.name,
			}
		).insert(ignore_permissions=True)

	def test_types_sizes_content_and_privacy(self):
		accepted = self.attach("delivery-note.pdf", PDF)
		self.assertTrue(
			frappe.db.get_value("File", accepted.name, "automotive_sha256")
		)  # recorded after insert
		with self.assertRaises(AttachmentRejectedError):  # not an allowed type
			self.attach("tool.exe", b"MZ\x90\x00")
		with self.assertRaises(AttachmentRejectedError):  # an executable named as a PDF
			self.attach("invoice.pdf", b"MZ\x90\x00\x03\x00\x00\x00" * 64)
		with self.assertRaises(AttachmentRejectedError):  # business attachments stay private
			self.attach("public.pdf", PDF, private=0)
		self._settings(max_upload_mb=1)
		with self.assertRaises(AttachmentRejectedError):
			self.attach("big.csv", b"x" * (1024 * 1024 + 1))

	def test_downloads_wait_for_a_clean_scan_when_scanning_is_required(self):
		self._settings(virus_scan_mode="Required", clamd_host="")
		doc = self.attach("drawing.pdf", PDF)
		self.assertEqual(
			frappe.db.get_value("File", doc.name, "automotive_scan_status"), "Scanner Not Configured"
		)
		doc.reload()
		with as_user(READER):
			self.assertFalse(policy.has_permission(doc, "read", READER))
		frappe.db.set_value("File", doc.name, "automotive_scan_status", "Clean")
		doc.reload()
		with as_user(READER):
			self.assertIsNone(policy.has_permission(doc, "read", READER))  # no objection: normal rules apply

	def test_files_without_a_result_are_scanned_by_the_hourly_job(self):
		from unittest import mock

		from automotive_core.files import clamav

		def status(doc):
			return frappe.db.get_value("File", doc.name, "automotive_scan_status")

		self._settings(virus_scan_mode="Required", clamd_host="")
		waiting = self.attach("received-before-scanning.pdf", PDF)
		self.assertEqual(status(waiting), "Scanner Not Configured")
		self._settings(clamd_host="clamd.invalid")
		with mock.patch.object(clamav, "ping", return_value=False):
			self.assertEqual(policy.rescan_pending(), 0)  # clamd does not answer: no new failures recorded
		self.assertEqual(status(waiting), "Scanner Not Configured")
		with (
			mock.patch.object(clamav, "ping", return_value=True),
			mock.patch.object(clamav, "scan_bytes", return_value=(True, None)),
		):
			self.assertGreaterEqual(policy.rescan_pending(), 1)
		self.assertEqual(status(waiting), "Clean")
		self._settings(virus_scan_mode="Disabled")
		self.assertEqual(policy.rescan_pending(), 0)

	def test_retention_by_rule_and_by_default(self):
		note = frappe.get_doc({"doctype": "Note", "title": "retention", "public": 1}).insert(
			ignore_permissions=True
		)
		old_todo = self.attach("old.pdf", PDF)
		new_todo = self.attach("new.pdf", PDF)
		old_note = self.attach("old-note.pdf", PDF, doctype="Note", docname=note.name)
		for name in (old_todo.name, old_note.name):
			frappe.db.set_value(
				"File", name, "creation", add_days(now_datetime(), -40), update_modified=False
			)
		self._settings(
			attachment_rules=[{"reference_doctype": "ToDo", "retention_days": 30, "private_only": 1}],
			default_retention_days=0,
		)
		policy.apply_retention()
		self.assertFalse(frappe.db.exists("File", old_todo.name))  # the ToDo rule: 30 days
		self.assertTrue(frappe.db.exists("File", new_todo.name))
		self.assertTrue(frappe.db.exists("File", old_note.name))  # no rule, default 0: kept
		self._settings(default_retention_days=30)
		policy.apply_retention()
		self.assertFalse(frappe.db.exists("File", old_note.name))  # the default period
		self.assertTrue(frappe.db.exists("Audit Event", {"event_type": "file.retention_purge"}))
