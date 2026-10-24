"""Bulk import through Frappe's Data Import (ERP-01.3, ops/runbooks/master-data.md): every row
is saved as a form save would be, so the duplicate rules refuse a duplicate row while the
other rows load, and the import log says why."""

from __future__ import annotations

import csv
import io
import json

import frappe
from frappe.tests import IntegrationTestCase


def _remove(doctype: str, names: list[str]) -> None:
	"""The importer commits row by row: what it (and the test before it) wrote stays unless
	removed."""
	for name in names:
		if doctype == "Data Import":
			frappe.db.delete("Data Import Log", {"data_import": name})
		frappe.delete_doc_if_exists(doctype, name, force=1)
	frappe.db.commit()


class TestBulkImport(IntegrationTestCase):
	def test_an_item_import_loads_new_rows_and_refuses_a_duplicate(self):
		suffix = frappe.generate_hash(length=6).upper()
		group = frappe.db.get_value("Item Group", {"is_group": 0}, "name", order_by="name asc")
		existing = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": f"BRK-{suffix}",
				"item_name": f"Brake Bracket {suffix}",
				"item_group": group,
				"stock_uom": "Nos",
				"is_stock_item": 1,
			}
		).insert(ignore_permissions=True)
		codes = [f"IMP-{suffix}-{i}" for i in (1, 2, 3)]
		self.addCleanup(_remove, "Item", [*codes, existing.name])
		rows = [
			("Item Code", "Item Name", "Item Group", "Default Unit of Measure"),
			(codes[0], f"Door Hinge {suffix}", group, "Nos"),
			(codes[1], f"Brake Bracket {suffix}", group, "Nos"),  # the existing item's name in its group
			(codes[2], f"Seat Rail {suffix}", group, "Nos"),
		]
		content = io.StringIO()
		csv.writer(content).writerows(rows)
		upload = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"items-{suffix}.csv",
				"content": content.getvalue(),
				"is_private": 1,
			}
		).save(ignore_permissions=True)
		self.addCleanup(_remove, "File", [upload.name])
		data_import = frappe.get_doc(
			{
				"doctype": "Data Import",
				"reference_doctype": "Item",
				"import_type": "Insert New Records",
				"import_file": upload.file_url,
			}
		).insert()
		frappe.db.commit()  # the import rolls back a failed row: what is set up here must not go with it
		self.addCleanup(_remove, "Data Import", [data_import.name])

		data_import.start_import()  # runs at once under tests, as a background job on a site

		self.assertEqual(data_import.reload().status, "Partial Success")
		self.assertTrue(frappe.db.exists("Item", codes[0]))
		self.assertTrue(frappe.db.exists("Item", codes[2]))
		self.assertFalse(frappe.db.exists("Item", codes[1]))
		logs = frappe.get_all(
			"Data Import Log",
			filters={"data_import": data_import.name},
			fields=["success", "messages", "exception"],
		)
		self.assertEqual(sorted(log.success for log in logs), [0, 1, 1])
		refused = next(log for log in logs if not log.success)
		reason = json.dumps(refused.messages or "") + (refused.exception or "")
		self.assertIn("already has the name", reason)
		self.assertIn(existing.name, reason)
