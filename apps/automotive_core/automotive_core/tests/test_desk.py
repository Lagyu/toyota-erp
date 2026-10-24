"""ERP-12 desk surfaces: workspaces resolve, KPI cards compute through the KPI service,
script reports run through Frappe's report runner and refuse out-of-scope companies."""

from __future__ import annotations

import json
from unittest.mock import patch

import frappe
from frappe.desk.query_report import run
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from automotive_core.analytics.cards import kpi_card
from automotive_core.tests.utils import AICHI, NA, as_user, make_user

WORKSPACES = ("Automotive Setup", "Governance", "Integration Hub", "Finance Controls", "Management")


class TestDesk(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for day in (add_days(nowdate(), -40), nowdate()):
			if not frappe.db.exists(
				"Currency Exchange", {"date": day, "from_currency": "USD", "to_currency": "JPY"}
			):
				frappe.get_doc(
					{
						"doctype": "Currency Exchange",
						"date": day,
						"from_currency": "USD",
						"to_currency": "JPY",
						"exchange_rate": 150,
						"for_buying": 1,
						"for_selling": 1,
					}
				).insert(ignore_permissions=True)
		cls.controller = make_user("controller.desk@hinode.example", ["Group Controller", "Accounts Manager"])
		cls.aichi_user = make_user("auditor.aichi.desk@hinode.example", ["Auditor"])
		if not frappe.db.exists("User Permission", {"user": cls.aichi_user, "allow": "Company"}):
			frappe.get_doc(
				{
					"doctype": "User Permission",
					"user": cls.aichi_user,
					"allow": "Company",
					"for_value": AICHI,
					"apply_to_all_doctypes": 1,
				}
			).insert(ignore_permissions=True)

	def test_every_report_a_persona_is_offered_runs_for_them(self):
		"""ERP-12.2, GATE-1: workspaces and sidebars offer the reports a user's roles may open; each
		of them must also run, which needs the right to report on its DocType (setup/permissions.py,
		align_report_roles)."""
		from frappe.desk.desk_views import DeskViews

		from automotive_core.setup.permissions import align_report_roles
		from automotive_core.setup.roles import ROLE_PROFILES

		align_report_roles()
		self.assertEqual(align_report_roles(), [])  # already aligned: nothing changes
		for profile, roles in ROLE_PROFILES.items():
			if profile.endswith("Portal User"):
				continue
			user = make_user(f"reports.{profile.lower().replace(' ', '-')}@hinode.example", roles)
			with as_user(user):
				offered = DeskViews.get_allowed_reports(user=user)
				dead = sorted(
					name
					for name, row in offered.items()
					if row.get("ref_doctype") and not frappe.has_permission(row["ref_doctype"], "report")
				)
			self.assertEqual(dead, [], profile)

	def test_workspaces_render(self):
		from frappe.desk.desktop import get_desktop_page

		for name in WORKSPACES:
			page = get_desktop_page(json.dumps({"name": name, "title": name}))
			self.assertIn("cards", page, name)
		governance = get_desktop_page(json.dumps({"name": "Governance", "title": "Governance"}))
		self.assertIn(
			"Pending Approvals", {c.get("number_card_name") for c in governance["number_cards"]["items"]}
		)

	def test_kpi_card_uses_the_kpi_service(self):
		with as_user(self.controller):
			card = kpi_card(json.dumps({"kpi_code": "FIN-DSO", "company": AICHI}))
		self.assertEqual(card["fieldtype"], "Float")
		self.assertIn("value", card)
		self.assertEqual(card["route"], ["Form", "KPI Definition", "FIN-DSO"])

	def test_consolidation_translates_and_balances_in_company_currency(self):
		with as_user(self.controller):
			result = run(
				"Group Consolidation", filters={"to_date": nowdate(), "presentation_currency": "JPY"}
			)
		lines = {row["line"]: row for row in result["result"]}
		self.assertIn("Currency Translation Difference", lines)
		aichi = frappe.scrub(AICHI)
		# JPY companies translate at 1: their translation difference is zero (debits = credits)
		self.assertAlmostEqual(lines["Currency Translation Difference"][aichi], 0, places=2)

	def test_reports_refuse_out_of_scope_company(self):
		with as_user(self.aichi_user):
			result = run("Group Consolidation", filters={"to_date": nowdate()})
			self.assertEqual(
				[c["label"] for c in result["columns"][1:2]],
				[frappe.get_cached_value("Company", AICHI, "abbr")],
			)
			with self.assertRaises(frappe.PermissionError):
				run("Group Consolidation", filters={"to_date": nowdate(), "company": NA})

	def test_sod_review_runs_for_auditors(self):
		with as_user(self.aichi_user):
			result = run("Segregation of Duties Review", filters={})
		self.assertIsInstance(result["result"], list)

	def test_approval_inbox_page_is_standard(self):
		page = frappe.get_doc("Page", "approval-inbox")
		self.assertEqual(page.module, "Governance")
		self.assertIn("Treasury Approver", {r.role for r in page.roles})


def _remove_prepared_report(name: str, user: str) -> None:
	"""The worker commits its status: what the test wrote stays unless removed."""
	frappe.db.delete("Notification Log", {"for_user": user, "document_type": "Report"})
	for file_name in frappe.get_all(
		"File", filters={"attached_to_doctype": "Prepared Report", "attached_to_name": name}, pluck="name"
	):
		frappe.delete_doc("File", file_name, force=1, ignore_permissions=True)
	frappe.delete_doc_if_exists("Prepared Report", name, force=1)
	frappe.db.commit()


class TestPreparedReports(IntegrationTestCase):
	def test_a_slow_report_runs_in_the_background_as_a_prepared_report(self):
		"""PRF-4: Frappe turns a script report into a prepared report once a run inside a request
		takes more than 15 s, and no report of the apps turns that off. A prepared report waits on
		the long queue with a stored status; the worker keeps the result for the user who asked for
		it and notifies them (deleting a queued or running one stops its job)."""
		from frappe.core.doctype.prepared_report.prepared_report import generate_report, make_prepared_report
		from frappe.desk.query_report import get_prepared_report_result

		modules = frappe.get_all("Module Def", filters={"app_name": ("like", "automotive_%")}, pluck="name")
		reports = frappe.get_all(
			"Report",
			filters={"module": ("in", modules), "report_type": "Script Report"},
			fields=["name", "disable_prepared_report_automation"],
		)
		self.assertGreaterEqual(len(reports), 14)
		self.assertEqual([r.name for r in reports if r.disable_prepared_report_automation], [])

		auditor = make_user("auditor.prepared@hinode.example", ["Auditor"])
		# the job is recorded, not handed to the test bench's worker: the test runs it itself
		with (
			as_user(auditor),
			patch("frappe.core.doctype.prepared_report.prepared_report.enqueue") as enqueue,
		):
			name = make_prepared_report("Segregation of Duties Review", {})["name"]
		self.addCleanup(_remove_prepared_report, name, auditor)
		self.assertEqual(frappe.db.get_value("Prepared Report", name, "status"), "Queued")
		self.assertEqual(
			(enqueue.call_args.kwargs["queue"], enqueue.call_args.kwargs["prepared_report"]), ("long", name)
		)
		with as_user(auditor):
			generate_report(name)  # what the long-queue worker runs
			result = get_prepared_report_result(
				frappe.get_doc("Report", "Segregation of Duties Review"), {}, name, auditor
			)
		self.assertEqual(result["doc"].status, "Completed")
		self.assertTrue(result["columns"])
		self.assertIsInstance(result["result"], list)
		self.assertTrue(
			frappe.db.exists(
				"Notification Log",
				{
					"for_user": auditor,
					"document_type": "Report",
					"document_name": "Segregation of Duties Review",
				},
			)
		)
