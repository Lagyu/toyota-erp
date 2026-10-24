"""Concurrent requests to critical workflows take effect once (TST-6, ERP-11).

Run against a reference stack whose personas and journey fixtures are prepared
(scripts/ci/e2e.sh: INTEGRATION_CREDENTIALS, INTEGRATION_FIXTURES):

- a portal write sent eight times at once with one Idempotency-Key creates one document,
  and every answer names it (or reports the first as still in progress);
- eight simultaneous redeliveries of one signed inbound message are accepted once: the
  others are answered as duplicates of the same stored message;
- eight simultaneous submits of one draft journal entry post it once.
"""

from __future__ import annotations

import datetime
import json
import os
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from integration_tests.contract import Client, encode_edi, sign

PARALLEL = 8
INBOUND = "/api/method/automotive_core.integration_hub.inbound.receive"


def _load(variable: str) -> dict | None:
	path = os.environ.get(variable)
	return json.loads(Path(path).read_text()) if path and Path(path).exists() else None


def at_once(fn, count: int = PARALLEL) -> list:
	with ThreadPoolExecutor(max_workers=count) as pool:
		return list(pool.map(fn, range(count)))


class TestConcurrency(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.credentials = _load("INTEGRATION_CREDENTIALS")
		cls.fixtures = _load("INTEGRATION_FIXTURES")

	def test_one_idempotency_key_sent_at_once_creates_one_document(self):
		dealer = (self.credentials or {}).get("users", {}).get("dealer")
		if not dealer:
			self.skipTest("no dealer persona (INTEGRATION_CREDENTIALS)")
		client = Client()
		client.login(dealer, self.credentials["password"])
		client.csrf("/dealer-portal")
		status, dashboard = client.request("GET", "/api/method/automotive_portals.api.dealer.dashboard")
		if "raise_support" not in ((dashboard or {}).get("message") or {}).get("capabilities", []):
			self.skipTest("the dealer persona's portal role cannot raise support requests")
		key, subject = str(uuid.uuid4()), f"[concurrency] {uuid.uuid4().hex[:10]}"
		body = json.dumps({"subject": subject, "description": "One request, sent eight times at once."}).encode()

		def send(_):
			return client.request("POST", "/api/method/automotive_portals.api.dealer.create_support_request", body=body,
				headers={"Content-Type": "application/json", "Idempotency-Key": key})

		answers = at_once(send)
		statuses = sorted(status for status, _ in answers)
		self.assertTrue(set(statuses) <= {200, 409}, statuses)  # 409: the first is still being processed
		created = {data["message"]["issue"] for status, data in answers if status == 200}
		self.assertEqual(len(created), 1, answers)
		status, again = send(0)  # a retry afterwards gets the stored answer
		self.assertEqual((status, again["message"]["issue"]), (200, next(iter(created))))
		status, rows = client.request("GET", "/api/method/automotive_portals.api.dealer.support_requests")
		self.assertEqual(sum(1 for row in rows["message"] if row["subject"] == subject), 1)

	def test_simultaneous_redeliveries_of_one_message_are_accepted_once(self):
		partner = (self.fixtures or {}).get("j10")
		if not partner:
			self.skipTest("no EDI partner fixture (INTEGRATION_FIXTURES)")
		# an announcement that cannot be processed (no lines): the journeys' own messages and
		# orders stay untouched, and storing it once is what is under test
		envelope = {"message_id": f"CONCURRENCY.{uuid.uuid4().hex[:12]}", "message_type": "asn", "schema_version": "v1",
			"data": {"supplier_asn_no": f"CONC-{uuid.uuid4().hex[:8]}", "lines": []}}
		edi = partner.get("format") == "Hinode EDI"
		body = (encode_edi(envelope) if edi else json.dumps(envelope)).encode()
		guest = Client()

		def deliver(_):
			return guest.request("POST", INBOUND, body=body, headers={
				"Content-Type": "text/plain" if edi else "application/json",
				"X-Hinode-Partner": partner["partner"], "X-Hinode-Signature": sign(partner["secret"], body)})

		answers = at_once(deliver)
		statuses = sorted(status for status, _ in answers)
		self.assertEqual(statuses.count(202), 1, answers)  # accepted once ...
		self.assertEqual(statuses.count(200), PARALLEL - 1, answers)  # ... the rest are duplicates
		results = [data["message"] for _, data in answers]
		self.assertEqual({r["status"] for r in results if r["status"] != "accepted"}, {"duplicate"})
		self.assertEqual(len({r["inbound_id"] for r in results}), 1)  # one stored message

	def test_simultaneous_submits_post_once(self):
		password = os.environ.get("INTEGRATION_PASSWORD")
		if not password:
			self.skipTest("no administrator password")
		admin = Client()
		admin.login("Administrator", password)
		admin.csrf("/app")

		def value(doctype, filters, field):
			_status, data = admin.request("GET", "/api/method/frappe.client.get_value", params={"doctype": doctype,
				"filters": json.dumps(filters), "fieldname": field})
			return ((data or {}).get("message") or {}).get(field)

		company = value("Company", {"abbr": "HAMJ"}, "name")
		cash = value("Account", {"company": company, "account_type": "Cash", "is_group": 0}, "name")
		expense = value("Account", {"company": company, "root_type": "Expense", "account_type": "", "is_group": 0}, "name")
		cost_center = value("Company", {"name": company}, "cost_center")
		draft = {"doctype": "Journal Entry", "company": company, "user_remark": "[concurrency]",
			"posting_date": datetime.date.today().isoformat(),
			"accounts": [{"account": expense, "debit_in_account_currency": 10, "cost_center": cost_center},
				{"account": cash, "credit_in_account_currency": 10, "cost_center": cost_center}]}
		status, saved = admin.request("POST", "/api/method/frappe.client.insert",
			body=json.dumps({"doc": draft}).encode(), headers={"Content-Type": "application/json"})
		self.assertEqual(status, 200, saved)
		doc = json.dumps({"doc": saved["message"]}).encode()

		def submit(_):
			return admin.request("POST", "/api/method/frappe.client.submit", body=doc,
				headers={"Content-Type": "application/json"})

		answers = at_once(submit)
		self.assertEqual(sorted(status for status, _ in answers).count(200), 1, [s for s, _ in answers])
		status, entries = admin.request("GET", "/api/method/frappe.client.get_count", params={"doctype": "GL Entry",
			"filters": json.dumps({"voucher_no": saved["message"]["name"], "is_cancelled": 0})})
		self.assertEqual(entries["message"], 2)  # one posting: a debit and a credit
