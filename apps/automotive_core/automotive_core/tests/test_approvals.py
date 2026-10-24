"""Approval engine: matrix resolution, maker/checker, distinct approvers, delegation,
document-hash binding and submit enforcement (ERP-05/09/10)."""

from __future__ import annotations

import datetime as dt

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from automotive_core.governance import approvals
from automotive_core.governance.approvals import ApprovalAuthorityError, ApprovalRequiredError
from automotive_core.tests.utils import AICHI, KYUSHU, as_user, make_policy, make_user, make_voucher

BUYER = "buyer.approvals@hinode.example"
PM1 = "pm1.approvals@hinode.example"
PM2 = "pm2.approvals@hinode.example"
DIRECTOR = "director.approvals@hinode.example"
DELEGATE = "delegate.approvals@hinode.example"
CONTROLLER = "controller.approvals@hinode.example"


class TestApprovals(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user(BUYER, ["Buyer"])
		make_user(PM1, ["Procurement Manager"])
		make_user(PM2, ["Procurement Manager"])
		make_user(DIRECTOR, ["Plant Director"])
		make_user(DELEGATE, ["Production Planner"])
		make_user(CONTROLLER, ["Group Controller"])
		make_policy("Test Voucher Matrix", [(1, "Procurement Manager", 0), (2, "Plant Director", 1_000_000)])
		approvals.clear_policy_cache()

	def request(self, doc, user=BUYER):
		with as_user(user):
			return approvals.request_approval(doc.doctype, doc.name)

	def decide(self, request, user, decision="Approve", comment=None):
		with as_user(user):
			return approvals.decide(request, decision, comment)

	def test_small_amount_skips_threshold_step_and_allows_submit(self):
		doc = make_voucher(BUYER, 50_000)
		req = self.request(doc)
		self.assertEqual([s["step_no"] for s in req["steps"]], [1])
		self.assertEqual(self.decide(req["name"], PM1)["status"], "Approved")
		with as_user(BUYER):
			doc.reload()
			doc.submit()
		self.assertEqual(doc.docstatus, 1)
		decision = frappe.get_all(
			"Approval Decision",
			filters={"approval_request": req["name"]},
			fields=["decided_by", "decision", "document_hash"],
		)
		self.assertEqual(decision[0].decided_by, PM1)

	def test_overdue_steps_escalate_once_to_the_next_level(self):
		"""A step pending past its escalation time (24 h from the policy) reminds its approvers
		and alerts the next step's approvers; after the last step, the group controllers."""
		doc = make_voucher(BUYER, 5_000_000)
		req = self.request(doc)["name"]
		requested_at = frappe.utils.get_datetime(frappe.db.get_value("Approval Request", req, "requested_at"))
		self.assertNotIn(req, approvals.escalate_overdue(requested_at + dt.timedelta(hours=23)))
		self.assertIn(req, approvals.escalate_overdue(requested_at + dt.timedelta(hours=25)))
		self.assertNotIn(
			req, approvals.escalate_overdue(requested_at + dt.timedelta(hours=30))
		)  # once per step

		def step(no):
			return frappe.get_all(
				"Approval Request Step",
				filters={"parent": req, "step_no": no},
				fields=["escalate_after_hours", "escalated_at", "escalated_to", "decided_at"],
			)[0]

		first = step(1)
		self.assertEqual(first.escalate_after_hours, 24)
		self.assertTrue(first.escalated_at)
		self.assertIn(DIRECTOR, first.escalated_to.split("\n"))  # the next step's approvers
		self.assertTrue(
			frappe.db.exists(
				"Notification Dispatch",
				{"event": "approval.escalated", "recipient": PM1, "reference_name": req, "channel": "In-App"},
			)
		)
		self.assertTrue(
			frappe.db.exists("Audit Event", {"event_type": "approval.escalated", "reference_name": doc.name})
		)

		# the last step's clock starts at the decision before it, and escalates to group control
		self.decide(req, PM1)
		decided_at = frappe.utils.get_datetime(step(1).decided_at)
		self.assertNotIn(req, approvals.escalate_overdue(decided_at + dt.timedelta(hours=20)))
		self.assertIn(req, approvals.escalate_overdue(decided_at + dt.timedelta(hours=25)))
		self.assertIn(CONTROLLER, step(2).escalated_to.split("\n"))

	def test_submit_without_approval_is_refused(self):
		doc = make_voucher(BUYER, 50_000)
		with as_user(BUYER), self.assertRaises(ApprovalRequiredError):
			doc.submit()

	def test_large_amount_needs_both_steps_in_order(self):
		doc = make_voucher(BUYER, 5_000_000)
		req = self.request(doc)
		self.assertEqual([s["step_no"] for s in req["steps"]], [1, 2])
		with self.assertRaises(ApprovalAuthorityError):
			self.decide(req["name"], DIRECTOR)  # step 1 belongs to procurement
		self.assertEqual(self.decide(req["name"], PM1)["status"], "Pending")
		with as_user(BUYER), self.assertRaises(ApprovalRequiredError):
			doc.reload()
			doc.submit()
		self.assertEqual(self.decide(req["name"], DIRECTOR)["status"], "Approved")

	def test_maker_cannot_approve_own_document(self):
		make_user("maker.pm@hinode.example", ["Procurement Manager", "Buyer"])
		doc = make_voucher("maker.pm@hinode.example", 10_000)
		req = self.request(doc, user="maker.pm@hinode.example")
		with self.assertRaises(ApprovalAuthorityError):
			self.decide(req["name"], "maker.pm@hinode.example")

	def test_distinct_approvers_across_steps(self):
		make_user("pm.director@hinode.example", ["Procurement Manager", "Plant Director"])
		doc = make_voucher(BUYER, 2_000_000)
		req = self.request(doc)
		self.decide(req["name"], "pm.director@hinode.example")
		with self.assertRaises(ApprovalAuthorityError):
			self.decide(req["name"], "pm.director@hinode.example")
		self.assertEqual(self.decide(req["name"], DIRECTOR)["status"], "Approved")

	def test_change_after_approval_invalidates(self):
		doc = make_voucher(BUYER, 40_000)
		req = self.request(doc)
		self.decide(req["name"], PM1)
		with as_user(BUYER):
			doc.reload()
			doc.items[0].qty = 20
			doc.grand_total = 80_000
			doc.save()
			with self.assertRaises(ApprovalRequiredError):
				doc.submit()
		# re-requesting starts a fresh cycle bound to the new content
		new_req = self.request(doc)
		self.assertNotEqual(new_req["name"], req["name"])
		self.assertEqual(frappe.db.get_value("Approval Request", req["name"], "status"), "Invalidated")

	def test_change_during_pending_invalidates_on_decision(self):
		doc = make_voucher(BUYER, 40_000)
		req = self.request(doc)
		with as_user(BUYER):
			doc.reload()
			doc.party = "Different Supplier KK"
			doc.save()
		result = self.decide(req["name"], PM1)
		self.assertEqual(result["status"], "Invalidated")

	def test_reject_requires_comment_and_ends_cycle(self):
		doc = make_voucher(BUYER, 40_000)
		req = self.request(doc)
		with self.assertRaises(frappe.ValidationError):
			self.decide(req["name"], PM1, "Reject")
		self.assertEqual(self.decide(req["name"], PM1, "Reject", "Price above quote")["status"], "Rejected")
		with self.assertRaises(frappe.ValidationError):
			self.decide(req["name"], PM2)

	def test_delegation_with_effective_dates(self):
		with as_user(PM2):
			delegation = frappe.get_doc(
				{
					"doctype": "Approval Delegation",
					"delegator": PM2,
					"delegate": DIRECTOR,
					"approver_role": "Procurement Manager",
					"from_date": nowdate(),
					"to_date": add_days(nowdate(), 5),
					"reason": "Business trip",
				}
			).insert()
			delegation.submit()
		self.addCleanup(self._revoke, delegation.name, PM2)
		self.assertEqual(delegation.status, "Active")
		doc = make_voucher(BUYER, 30_000)
		req = self.request(doc)
		result = self.decide(req["name"], DIRECTOR)
		self.assertEqual(result["status"], "Approved")
		step = frappe.get_doc("Approval Request", req["name"]).steps[0]
		self.assertEqual(step.decided_by, DIRECTOR)
		self.assertEqual(step.on_behalf_of, PM2)

	def _revoke(self, name, user):
		doc = frappe.get_doc("Approval Delegation", name)
		if doc.docstatus == 1:
			with as_user(user):
				doc.flags.cancel_reason = "test cleanup"
				doc.cancel()

	def test_revoked_or_out_of_scope_delegation_grants_nothing(self):
		with as_user(PM1):
			delegation = frappe.get_doc(
				{
					"doctype": "Approval Delegation",
					"delegator": PM1,
					"delegate": DELEGATE,
					"approver_role": "Procurement Manager",
					"from_date": nowdate(),
					"to_date": add_days(nowdate(), 2),
					"scope_company": KYUSHU,
					"reason": "Covering Kyushu only",
				}
			).insert()
			delegation.submit()
		doc = make_voucher(BUYER, 30_000, company=AICHI)
		req = self.request(doc)
		with self.assertRaises(ApprovalAuthorityError):
			self.decide(req["name"], DELEGATE)  # company out of scope
		with as_user(PM1):
			delegation.reload()
			delegation.flags.cancel_reason = "Trip cancelled"
			delegation.cancel()
		kyushu_doc = make_voucher(BUYER, 30_000, company=KYUSHU)
		kyushu_req = self.request(kyushu_doc)
		with self.assertRaises(ApprovalAuthorityError):
			self.decide(kyushu_req["name"], DELEGATE)  # revoked

	def test_delegate_cannot_be_self_or_unheld_role(self):
		with as_user(PM1), self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Approval Delegation",
					"delegator": PM1,
					"delegate": PM1,
					"approver_role": "Procurement Manager",
					"from_date": nowdate(),
					"to_date": nowdate(),
					"reason": "x",
				}
			).insert()
		with as_user(PM1), self.assertRaises(frappe.ValidationError):
			frappe.get_doc(
				{
					"doctype": "Approval Delegation",
					"delegator": PM1,
					"delegate": DIRECTOR,
					"approver_role": "Treasury Approver",
					"from_date": nowdate(),
					"to_date": nowdate(),
					"reason": "role not held",
				}
			).insert()

	def test_delegation_cannot_break_segregation_of_duties(self):
		from automotive_core.governance.delegation import delegated_roles
		from automotive_core.governance.sod import SegregationOfDutiesError

		treasurer = make_user("treasury.sod@hinode.example", ["Treasury Approver"])
		if not frappe.db.exists(
			"Segregation of Duties Rule", {"role_a": "Buyer", "role_b": "Treasury Approver"}
		):
			frappe.get_doc(
				{
					"doctype": "Segregation of Duties Rule",
					"rule_name": "Buyer vs Treasury Approver (test)",
					"role_a": "Buyer",
					"role_b": "Treasury Approver",
					"enforcement": "Block",
					"is_active": 1,
					"rationale": "Who orders must not release the payment.",
				}
			).insert(ignore_permissions=True)
		with as_user(treasurer), self.assertRaises(SegregationOfDutiesError):
			frappe.get_doc(
				{
					"doctype": "Approval Delegation",
					"delegator": treasurer,
					"delegate": BUYER,
					"approver_role": "Treasury Approver",
					"from_date": nowdate(),
					"to_date": nowdate(),
					"reason": "holiday cover",
				}
			).insert()
		# granted to someone without the conflict, then the delegate gains the conflicting role: no authority
		cover = make_user("cover.sod@hinode.example", ["Production Planner"])
		with as_user(treasurer):
			granted = frappe.get_doc(
				{
					"doctype": "Approval Delegation",
					"delegator": treasurer,
					"delegate": cover,
					"approver_role": "Treasury Approver",
					"from_date": nowdate(),
					"to_date": nowdate(),
					"reason": "holiday cover",
				}
			).insert()
			granted.submit()
		self.assertEqual([d.role for d in delegated_roles(cover)], ["Treasury Approver"])
		user = frappe.get_doc("User", cover)
		user.append("roles", {"role": "Buyer"})
		user.flags.ignore_permissions = True
		user.save()
		self.assertEqual(delegated_roles(cover), [])

	def test_inbox_lists_only_actionable_requests(self):
		doc = make_voucher(BUYER, 3_000_000)
		req = self.request(doc)
		with as_user(PM1):
			self.assertIn(req["name"], approvals.actionable_for())
		with as_user(DIRECTOR):
			self.assertNotIn(req["name"], approvals.actionable_for())  # step 2 not yet current
		with as_user(BUYER):
			self.assertNotIn(req["name"], approvals.actionable_for())  # requester

	def test_decisions_are_immutable(self):
		doc = make_voucher(BUYER, 20_000)
		req = self.request(doc)
		self.decide(req["name"], PM1)
		name = frappe.get_all("Approval Decision", filters={"approval_request": req["name"]}, pluck="name")[0]
		decision = frappe.get_doc("Approval Decision", name)
		decision.comment = "tampered"
		with self.assertRaises(frappe.PermissionError):
			decision.save(ignore_permissions=True)
		with self.assertRaises(Exception):  # DB trigger blocks direct updates too
			frappe.db.sql("update `tabApproval Decision` set comment='x' where name=%s", name)

	def test_request_is_idempotent_for_unchanged_document(self):
		doc = make_voucher(BUYER, 20_000)
		first = self.request(doc)
		second = self.request(doc)
		self.assertEqual(first["name"], second["name"])
