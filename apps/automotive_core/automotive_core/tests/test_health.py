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
		self._request_with({"X-Hinode-Metrics-Token": "anything"})
		self.assertEqual(health.metrics(), {"status": "forbidden"})
		frappe.local.conf["automotive_metrics_token"] = "s3cret-token"
		self._request_with({"X-Hinode-Metrics-Token": "wrong"})
		self.assertEqual(health.metrics(), {"status": "forbidden"})
		# the token in Authorization is not accepted: Frappe would take it for a login first
		self._request_with({"Authorization": "Bearer s3cret-token"})
		self.assertEqual(health.metrics(), {"status": "forbidden"})
		self.assertEqual(frappe.local.response.http_status_code, 403)

	def test_metrics_exposition(self):
		frappe.local.conf["automotive_metrics_token"] = "s3cret-token"
		self._request_with({"X-Hinode-Metrics-Token": "s3cret-token"})
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

	def test_request_latency_histogram(self):
		from automotive_core.observability import timing

		def samples():
			return {(m[0], m[2].get("le") or m[2].get("class"), m[2]["kind"], m[2].get("verb")): m[3]
				for m in timing.metrics()}

		before = samples()
		timing.observe(0.3, "/api/method/automotive_portals.api.dealer.place_order", "POST", 201)
		timing.observe(0.1, "/api/method/automotive_portals.api.dealer.orders", "GET", 503)
		after = samples()
		name = "erp_http_request_duration_seconds"
		delta = {key: after[key] - before[key] for key in after}
		self.assertEqual(delta[(f"{name}_bucket", "0.25", "api", "write")], 0)
		self.assertEqual(delta[(f"{name}_bucket", "0.5", "api", "write")], 1)  # cumulative buckets
		self.assertEqual(delta[(f"{name}_bucket", "+Inf", "api", "write")], 1)
		self.assertEqual(delta[(f"{name}_count", None, "api", "write")], 1)
		self.assertAlmostEqual(delta[(f"{name}_sum", None, "api", "write")], 0.3, places=6)
		self.assertEqual(delta[(f"{name}_count", None, "api", "read")], 1)
		# responses by status class, for the server error rate alert
		self.assertEqual(delta[("erp_http_responses_total", "2xx", "api", None)], 1)
		self.assertEqual(delta[("erp_http_responses_total", "5xx", "api", None)], 1)
		self.assertEqual(delta[("erp_http_responses_total", "4xx", "api", None)], 0)
		frappe.local.conf["automotive_metrics_token"] = "s3cret-token"
		self._request_with({"X-Hinode-Metrics-Token": "s3cret-token"})
		health.metrics()
		body = frappe.local.response.filecontent
		self.assertIn(f"# TYPE {name} histogram", body)
		self.assertEqual(body.count(f"# TYPE {name} "), 1)  # one header for the family
		self.assertIn(f'{name}_bucket{{kind="api",le="+Inf",site="{frappe.local.site}",verb="write"}}', body)
