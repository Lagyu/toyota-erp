"""Restore drill checks (ops/backup/restore-drill.sh): a restored copy must have every stored
file of its records and secrets that decrypt with the site's key."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.password import set_encrypted_password

from automotive_core.ops import backup_checks


class TestRestoreChecks(IntegrationTestCase):
	def test_a_lost_attachment_is_reported(self):
		baseline = backup_checks._attachments()
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": "lost.pdf",
				"is_private": 1,
				"file_url": "/private/files/lost-in-restore.pdf",
			}
		).db_insert()
		result = backup_checks._attachments()
		self.assertFalse(result["ok"])
		self.assertEqual(result["missing"], baseline["missing"] + 1)
		self.assertEqual(result["files"], baseline["files"] + 1)

	def test_a_secret_that_no_longer_decrypts_is_reported(self):
		set_encrypted_password("User", "Administrator", "probe-secret", "automotive_restore_probe")
		self.assertTrue(backup_checks._encrypted_fields()["ok"])
		# as if restored without the source site's key: the stored ciphertext is unreadable
		frappe.db.sql(
			"""update `__Auth` set password = %s where doctype = 'User' and name = 'Administrator'
			and fieldname = 'automotive_restore_probe'""",
			"gAAAAABnot-a-valid-token",
		)
		result = backup_checks._encrypted_fields()
		self.assertFalse(result["ok"])
		self.assertIn("User Administrator automotive_restore_probe", result["failed"])

	def test_the_drill_result_lists_every_check(self):
		result = backup_checks.verify_restored_site()
		names = {c["check"] for c in result["checks"]}
		self.assertTrue(
			{"audit hash chain", "record counts", "attachments restored", "encrypted fields decrypt"} <= names
		)
