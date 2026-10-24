"""Request limits of the partner portals and the shop floor (SEC-5): per signed-in user and
minute, refused with HTTP 429 before the method runs (access/request_budgets.py)."""

from __future__ import annotations

import datetime
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from automotive_core.access import request_budgets
from automotive_core.tests.utils import as_user, make_user

MINUTE = datetime.datetime(2031, 4, 5, 6, 7, 8)
LATER = MINUTE + datetime.timedelta(minutes=1)
ACKNOWLEDGE = "automotive_portals.api.supplier.acknowledge"
SUPPLIER_A = "budget.supplier-a@partner.example"
SUPPLIER_B = "budget.supplier-b@partner.example"
OPERATOR = "budget.operator@hinode.example"


class TestRequestBudgets(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for user in (SUPPLIER_A, SUPPLIER_B):
			make_user(user, [], user_type="Website User")
		make_user(OPERATOR, ["Line Operator"])

	def setUp(self):
		self._request = getattr(frappe.local, "request", None)
		self.addCleanup(setattr, frappe.local, "request", self._request)
		for field, value in (("portal_changes_per_minute", 2), ("portal_reads_per_minute", 0),
				("shop_floor_requests_per_minute", 3)):
			previous = frappe.db.get_single_value("Automotive Settings", field)
			frappe.db.set_single_value("Automotive Settings", field, value)
			self.addCleanup(frappe.db.set_single_value, "Automotive Settings", field, previous)
		# the counters live in Redis (not rolled back with the test): minutes no other test uses
		keys = [frappe.cache.make_key(f"automotive:budget:{group}:{user}:{at:%Y%m%d%H%M}")
			for group in request_budgets.LIMITS for user in (SUPPLIER_A, SUPPLIER_B, OPERATOR)
			for at in (MINUTE, LATER)]
		frappe.cache.delete(*keys)
		self.addCleanup(frappe.cache.delete, *keys)

	def call(self, user: str, method: str, http_method: str = "POST", at: datetime.datetime = MINUTE) -> None:
		"""What the web worker does before it runs ``method`` for ``user``."""
		frappe.local.request = Request(EnvironBuilder(path=f"/api/method/{method}", method=http_method).get_environ())
		with as_user(user), mock.patch.object(request_budgets, "now_datetime", return_value=at):
			request_budgets.before_request()

	def test_portal_changes_over_the_limit_are_refused_per_user_until_the_next_minute(self):
		self.call(SUPPLIER_A, ACKNOWLEDGE)
		self.call(SUPPLIER_A, "automotive_portals.api.supplier.create_asn")
		with self.assertRaises(frappe.TooManyRequestsError):
			self.call(SUPPLIER_A, ACKNOWLEDGE)
		self.assertEqual(frappe.TooManyRequestsError.http_status_code, 429)
		# another user of the same partner office keeps their own budget
		self.call(SUPPLIER_B, ACKNOWLEDGE)
		# reads have a limit of their own (here the default, 300)
		for _ in range(5):
			self.call(SUPPLIER_A, "automotive_portals.api.supplier.orders", "GET")
		# the next minute starts afresh
		self.call(SUPPLIER_A, ACKNOWLEDGE, at=LATER)

	def test_partner_uploads_count_as_portal_changes(self):
		self.assertEqual(request_budgets.group_of("upload_file", "POST", SUPPLIER_A), "portal-change")
		self.assertIsNone(request_budgets.group_of("upload_file", "POST", OPERATOR))  # desk users are not limited
		self.call(SUPPLIER_A, "upload_file")
		self.call(SUPPLIER_A, "upload_file")
		with self.assertRaises(frappe.TooManyRequestsError):
			self.call(SUPPLIER_A, "upload_file")

	def test_the_shop_floor_has_its_limit_and_desk_methods_have_none(self):
		for _ in range(3):
			self.call(OPERATOR, "automotive_manufacturing.mes.production.record_operation")
		with self.assertRaises(frappe.TooManyRequestsError):
			self.call(OPERATOR, "automotive_manufacturing.shop_floor.andon.acknowledge")
		for _ in range(10):
			self.call(OPERATOR, "frappe.client.get_list", "GET")
		self.assertIn("automotive_core.access.request_budgets.before_request", frappe.get_hooks("before_request"))
