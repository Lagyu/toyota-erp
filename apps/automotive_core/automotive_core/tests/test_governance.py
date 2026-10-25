"""Audit chain, cancel-with-reason, segregation of duties and account provisioning."""

from __future__ import annotations

import itertools

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, now_datetime, nowdate

from automotive_core.governance import audit, cancellation, provisioning
from automotive_core.governance.cancellation import CancelReasonRequiredError
from automotive_core.governance.sod import SegregationOfDutiesError
from automotive_core.tests.utils import TEST_VOUCHER, as_user, make_user, make_voucher
from automotive_core.utils import hook_set, json_argument, requested_doc


class TestAuditChain(IntegrationTestCase):
	def test_events_seal_into_a_verifiable_chain(self):
		names = [audit.record("test.event", payload={"n": i}) for i in range(5)]
		audit.seal_pending()
		rows = frappe.get_all(
			"Audit Event",
			filters={"name": ("in", names)},
			fields=["name", "chain_index", "previous_hash", "event_hash"],
			order_by="chain_index asc",
		)
		self.assertTrue(all(r.event_hash for r in rows))
		for prev, cur in itertools.pairwise(rows):
			self.assertEqual(cur.previous_hash, prev.event_hash)
			self.assertEqual(cur.chain_index, prev.chain_index + 1)
		result = audit.verify_chain()
		self.assertTrue(result["ok"], result)

	def test_sealed_events_cannot_be_changed_or_deleted(self):
		name = audit.record("test.immutable", payload={"x": 1})
		audit.seal_pending()
		with self.assertRaises(Exception):
			frappe.db.sql("update `tabAudit Event` set payload='{}' where name=%s", name)
		with self.assertRaises(Exception):
			frappe.db.sql("delete from `tabAudit Event` where name=%s", name)
		doc = frappe.get_doc("Audit Event", name)
		with self.assertRaises(frappe.PermissionError):
			doc.delete(ignore_permissions=True)

	def test_verifier_detects_a_forged_link(self):
		audit.record("test.before_forgery")
		audit.seal_pending()
		last = frappe.db.sql("select max(chain_index) from `tabAudit Event`")[0][0]
		# Inserts are not blocked by triggers; a forged, already "sealed" row breaks the chain.
		frappe.db.sql(
			"""insert into `tabAudit Event` (name, creation, modified, owner, modified_by, event_type, actor,
			event_time, payload, chain_index, previous_hash, event_hash)
			values (%s, %s, %s, 'Administrator', 'Administrator', 'forged', 'Administrator', %s, '{}', %s, %s, %s)""",
			(10**9, now_datetime(), now_datetime(), now_datetime(), last + 1, "0" * 64, "f" * 64),
		)
		result = audit.verify_chain()
		self.assertFalse(result["ok"])
		self.assertIn(str(last + 1), result["first_break"])


class TestCancellation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user("canceller@hinode.example", ["System Manager"])

	def test_governed_cancel_requires_reason_and_is_audited(self):
		self.assertIn("Journal Entry", hook_set("automotive_cancel_reason_doctypes"))
		with self.patch_hooks({"automotive_cancel_reason_doctypes": [TEST_VOUCHER]}):
			frappe.local.automotive_hook_sets = {}
			doc = make_voucher("Administrator", 100)
			doc.submit()
			with self.assertRaises(CancelReasonRequiredError):
				doc.cancel()
			with self.assertRaises(frappe.ValidationError):
				cancellation.cancel_with_reason(TEST_VOUCHER, doc.name, "short")
			cancellation.cancel_with_reason(TEST_VOUCHER, doc.name, "Duplicate voucher entered twice")
			self.assertEqual(frappe.db.get_value(TEST_VOUCHER, doc.name, "docstatus"), 2)
			event = frappe.get_all(
				"Audit Event",
				filters={"reference_name": doc.name, "event_type": "document.cancelled"},
				fields=["payload"],
			)
			self.assertIn("Duplicate voucher entered twice", event[0].payload)
		frappe.local.automotive_hook_sets = {}


class TestRequestArguments(IntegrationTestCase):
	"""TST-4: what a request names or sends in the wrong shape is refused as the caller's mistake
	(404, 417), not a server error."""

	def test_json_arguments_are_parsed_or_refused(self):
		self.assertEqual(json_argument('[{"item_code": "X"}]', "lines", list), [{"item_code": "X"}])
		self.assertEqual(json_argument([1], "lines", list), [1])
		self.assertIsNone(json_argument(" ", "lines", list))
		for value in ("CONTRACT-PLACEHOLDER", '{"a": 1}', "[1"):
			with self.assertRaises(frappe.ValidationError, msg=value):
				json_argument(value, "lines", list)

	def test_an_unknown_doctype_is_not_found(self):
		with self.assertRaises(frappe.DoesNotExistError):
			requested_doc("No Such Doctype Anywhere", "X")
		with self.assertRaises(frappe.DoesNotExistError):
			cancellation.cancel_with_reason("No Such Doctype Anywhere", "X", "A reason long enough to pass")


class TestSegregationOfDuties(IntegrationTestCase):
	def test_blocking_rule_prevents_conflicting_roles(self):
		with self.assertRaises(SegregationOfDutiesError):
			make_user("sod.conflict@hinode.example", ["Buyer", "AP Clerk"])

	def test_warning_rule_allows_but_audits(self):
		make_user("sod.warn@hinode.example", ["Warehouse Clerk", "Quality Manager"])
		self.assertTrue(
			frappe.db.exists(
				"Audit Event",
				{"event_type": "sod.warning_accepted", "reference_name": "sod.warn@hinode.example"},
			)
		)


class TestProvisioning(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user("it.maker@hinode.example", ["IT Operator", "System Manager"])
		make_user("it.checker@hinode.example", ["IT Operator", "System Manager"])

	def _request(self, **values):
		with as_user("it.maker@hinode.example"):
			doc = frappe.get_doc(
				{"doctype": "User Provisioning Request", "justification": "New hire", **values}
			).insert()
		return doc

	def _approve_and_submit(self, doc):
		from automotive_core.governance import approvals

		with as_user("it.maker@hinode.example"):
			req = approvals.request_approval(doc.doctype, doc.name)
		with as_user("it.checker@hinode.example"):
			approvals.decide(req["name"], "Approve")
		with as_user("it.maker@hinode.example"):
			doc.reload()
			doc.submit()
		return doc

	def test_create_requires_approval_then_applies_profile_and_scope(self):
		doc = self._request(
			request_type="Create",
			email="new.planner@hinode.example",
			first_name="Ren",
			role_profile="Production Planner",
			scopes=[{"allow": "Plant", "for_value": "AICHI-VA", "is_default": 1}],
		)
		from automotive_core.governance.approvals import ApprovalRequiredError

		with as_user("it.maker@hinode.example"), self.assertRaises(ApprovalRequiredError):
			doc.submit()
		self._approve_and_submit(doc)
		user = frappe.get_doc("User", "new.planner@hinode.example")
		self.assertIn("Production Planner", [r.role for r in user.roles])
		self.assertTrue(
			frappe.db.exists(
				"User Permission",
				{
					"user": user.name,
					"allow": "Plant",
					"for_value": "AICHI-VA",
					"automotive_managed_by": provisioning.MANAGED_BY,
				},
			)
		)

	def test_requested_roles_must_respect_segregation_of_duties(self):
		with self.assertRaises(SegregationOfDutiesError):
			self._request(
				request_type="Create",
				email="bad.combo@hinode.example",
				first_name="Bad",
				extra_roles=[{"role": "Buyer"}, {"role": "AP Clerk"}],
			)

	def test_deactivate_disables_and_expiry_job_disables(self):
		make_user("leaver@hinode.example", ["Buyer"])
		doc = self._request(
			request_type="Deactivate", user="leaver@hinode.example", email="leaver@hinode.example"
		)
		self._approve_and_submit(doc)
		self.assertEqual(frappe.db.get_value("User", "leaver@hinode.example", "enabled"), 0)
		make_user("contractor@hinode.example", ["Buyer"])
		frappe.db.set_value(
			"User", "contractor@hinode.example", "automotive_access_expiry_date", add_days(nowdate(), -1)
		)
		# an account that expires later, and one without an expiry date, stay enabled
		make_user("temporary@hinode.example", ["Buyer"])
		frappe.db.set_value(
			"User", "temporary@hinode.example", "automotive_access_expiry_date", add_days(nowdate(), 30)
		)
		make_user("permanent@hinode.example", ["Buyer"])
		frappe.db.set_value("User", "permanent@hinode.example", "automotive_access_expiry_date", None)
		provisioning.expire_accounts()
		self.assertEqual(frappe.db.get_value("User", "contractor@hinode.example", "enabled"), 0)
		for kept in ("temporary@hinode.example", "permanent@hinode.example", "it.maker@hinode.example"):
			self.assertEqual(frappe.db.get_value("User", kept, "enabled"), 1, kept)
