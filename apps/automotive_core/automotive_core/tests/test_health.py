"""Ops endpoints: liveness, readiness and token-protected Prometheus metrics."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from automotive_core.ops import health


class TestHealth(IntegrationTestCase):
	def setUp(self):
		self._request = getattr(frappe.local, "request", None)
		self._token = frappe.local.conf.get("automotive_metrics_token")

	def tearDown(self):
		frappe.local.request = self._request
		frappe.local.conf["automotive_metrics_token"] = self._token
		frappe.local.response = frappe._dict({"docs": []})

	def _request_with(self, headers: dict):
		frappe.local.request = Request(EnvironBuilder(path="/api/method/x", headers=headers).get_environ())

	def test_live_and_ready(self):
		self.assertEqual(health.live(), {"status": "ok"})
		result = health.ready()
		self.assertEqual(result["status"], "ok", result)
		self.assertEqual(set(result["checks"]), {"database", "cache", "queue", "scheduler"})
		self.assertTrue(all(c["ok"] for c in result["checks"].values()))

	def test_metrics_need_a_configured_token(self):
		frappe.local.conf["automotive_metrics_token"] = None
		self._request_with({"Authorization": "Bearer anything"})
		self.assertEqual(health.metrics(), {"status": "forbidden"})
		frappe.local.conf["automotive_metrics_token"] = "s3cret-token"
		self._request_with({"Authorization": "Bearer wrong"})
		self.assertEqual(health.metrics(), {"status": "forbidden"})
		self.assertEqual(frappe.local.response.http_status_code, 403)

	def test_metrics_exposition(self):
		frappe.local.conf["automotive_metrics_token"] = "s3cret-token"
		self._request_with({"Authorization": "Bearer s3cret-token"})
		health.metrics()
		body = frappe.local.response.filecontent
		self.assertIn("# TYPE erp_queue_jobs gauge", body)
		self.assertIn('erp_outbox_messages{site="' + frappe.local.site + '",status="Dead Letter"}', body)
		self.assertIn("erp_quality_holds_active", body)  # contributed by automotive_manufacturing
		self.assertIn("erp_match_exceptions_open", body)  # contributed by automotive_supply_chain
		for line in body.splitlines():
			if line and not line.startswith("#"):
				name, _, value = line.rpartition(" ")
				float(value)
				self.assertRegex(name, r"^erp_[a-z0-9_]+\{.*\}$")
