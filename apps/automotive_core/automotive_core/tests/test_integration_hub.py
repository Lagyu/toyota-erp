"""Integration hub: signing, EDI, idempotent inbox (J-10), replay and rate limits on the signed
endpoint, outbox delivery and allowlist."""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from unittest import mock

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from automotive_core.integration_hub import inbound, outbox, signing
from automotive_core.integration_hub.adapters import edi
from automotive_core.integration_hub.adapters.base import DeliveryResult
from automotive_core.integration_hub.adapters.file_drop import partner_dir
from automotive_core.tests.utils import make_user

HANDLER_CALLS: list[str] = []


def ok_handler(message, partner, data):
	"""Test handler: creates a ToDo so the business effect is countable."""
	HANDLER_CALLS.append(message.message_id)
	todo = frappe.get_doc({"doctype": "ToDo", "description": f"inbound {data['ref']}"}).insert()
	return "ToDo", todo.name


def flaky_handler(message, partner, data):
	HANDLER_CALLS.append(message.message_id)
	frappe.get_doc({"doctype": "ToDo", "description": f"half done {data['ref']}"}).insert()
	raise RuntimeError("downstream unavailable")


def envelope(message_id: str, ref: str = "R1", message_type: str = "production_event") -> bytes:
	return json.dumps({"message_id": message_id, "message_type": message_type, "data": {"ref": ref}}).encode()


class TestSigningAndEdi(UnitTestCase):
	def test_signature_round_trip_and_rejections(self):
		body = b'{"a":1}'
		header = signing.build_header("k1", "s3cret", body, timestamp=1_700_000_000, nonce="abc123nonce0")
		sig = signing.verify(header, body, "s3cret", 300, now=1_700_000_100)
		self.assertEqual(sig.key_id, "k1")
		for bad_header, bad_body, now in (
			(header, b'{"a":2}', 1_700_000_100),  # body changed
			(header, body, 1_700_000_400),  # outside tolerance
			(header.replace("v1=", "v1=0"), body, 1_700_000_100),
		):
			with self.assertRaises(signing.SignatureError):
				signing.verify(bad_header, bad_body, "s3cret", 300, now=now)
		with self.assertRaises(signing.SignatureError):
			signing.verify(header, body, "other-secret", 300, now=1_700_000_100)

	def test_edi_round_trip_with_reserved_characters(self):
		original = {
			"message_id": "ASN-0001",
			"message_type": "asn",
			"schema_version": "v1",
			"sent_at": "2026-04-01T09:00:00",
			"data": {
				"supplier": "Kisaragi*Precision~KK",
				"lines": [
					{"item": "BRK-001", "qty": 120, "weight": 3.5, "lot": "L?1"},
					{"item": "X", "qty": 1},
				],
				"urgent": True,
				"note": None,
				"empty": {},
			},
		}
		decoded = edi.decode(edi.encode(original))
		self.assertEqual(decoded, original)

	def test_edi_detects_truncation(self):
		text = edi.encode({"message_id": "M1", "message_type": "asn", "data": {"a": 1, "b": 2}})
		truncated = "\n".join(line for line in text.splitlines() if "DTA*b" not in line)
		with self.assertRaises(edi.EdiError):
			edi.decode(truncated)


class TestInbound(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user("edi.service@hinode.example", ["System Manager"])
		frappe.get_doc(
			{
				"doctype": "Integration Partner",
				"partner_code": "TEST-PLANT-MES",
				"partner_name": "Test MES",
				"partner_type": "Internal",
				"adapter": "Local File Drop",
				"message_format": "JSON",
				"allowed_message_types": "production_event\nasn",
				"service_user": "edi.service@hinode.example",
			}
		).insert(ignore_permissions=True)

	def setUp(self):
		HANDLER_CALLS.clear()
		frappe.flags.automotive_pending_inbound = []

	def handlers(self, path):
		frappe.local.automotive_hook_sets = {}
		return self.patch_hooks({"automotive_inbound_handlers": {"production_event": [path]}})

	def test_duplicate_delivery_has_exactly_one_business_effect(self):
		with self.handlers("automotive_core.tests.test_integration_hub.ok_handler"):
			first = inbound.accept("TEST-PLANT-MES", envelope("EVT-1"))
			again = inbound.accept("TEST-PLANT-MES", envelope("EVT-1"))
			self.assertEqual(first["status"], "accepted")
			self.assertEqual(again["status"], "duplicate")
			self.assertEqual(again["inbound_id"], first["inbound_id"])
			self.assertEqual(inbound.run_pending_inline(), ["Processed"])
			# a redelivery after processing reports the original result
			third = inbound.accept("TEST-PLANT-MES", envelope("EVT-1"))
			self.assertEqual(third["processing_status"], "Processed")
			self.assertEqual(third["result_doctype"], "ToDo")
			self.assertEqual(inbound.process(first["inbound_id"]), "Processed")  # re-run is a no-op
		self.assertEqual(HANDLER_CALLS, ["EVT-1"])
		self.assertEqual(frappe.db.count("ToDo", {"description": "inbound R1"}), 1)

	def test_same_id_different_payload_is_a_conflict(self):
		inbound.accept("TEST-PLANT-MES", envelope("EVT-2", ref="A"))
		with self.assertRaises(inbound.PayloadConflictError):
			inbound.accept("TEST-PLANT-MES", envelope("EVT-2", ref="B"))

	def test_failure_rolls_back_partial_work_then_retries_and_dead_letters(self):
		frappe.db.set_single_value("Automotive Settings", "inbound_max_attempts", 3)
		frappe.clear_document_cache("Automotive Settings", "Automotive Settings")
		with self.handlers("automotive_core.tests.test_integration_hub.flaky_handler"):
			name = inbound.accept("TEST-PLANT-MES", envelope("EVT-3", ref="F"))["inbound_id"]
			statuses = [inbound.process(name) for _ in range(3)]
		self.assertEqual(statuses, ["Failed", "Failed", "Dead Letter"])
		self.assertEqual(
			frappe.db.count("ToDo", {"description": "half done F"}), 0
		)  # partial effect rolled back
		msg = frappe.get_doc("Integration Inbound Message", name)
		self.assertEqual(msg.attempts, 3)
		self.assertIn("downstream unavailable", msg.last_error)
		# after the cause is fixed, a replay processes it exactly once
		with self.handlers("automotive_core.tests.test_integration_hub.ok_handler"):
			inbound.replay(name)
			inbound.run_pending_inline()
		self.assertEqual(frappe.db.get_value("Integration Inbound Message", name, "status"), "Processed")
		self.assertEqual(frappe.db.count("ToDo", {"description": "inbound F"}), 1)
		self.assertTrue(inbound.reconciliation()["ok"])

	def test_unlisted_message_type_and_bad_envelope_are_refused(self):
		with self.assertRaises(frappe.PermissionError):
			inbound.accept("TEST-PLANT-MES", envelope("EVT-4", message_type="supplier_invoice"))
		with self.assertRaises(frappe.ValidationError):
			inbound.accept(
				"TEST-PLANT-MES", b'{"message_id": "bad id with spaces", "message_type": "asn", "data": {}}'
			)

	def test_file_drop_inbox_is_polled_and_moved(self):
		from automotive_core.integration_hub.adapters.file_drop import poll_inbox

		base = partner_dir("TEST-PLANT-MES")
		(base / "inbox" / "evt-5.json").write_bytes(envelope("EVT-5"))
		(base / "inbox" / "junk.json").write_bytes(b"not json")
		counts = poll_inbox("TEST-PLANT-MES")
		self.assertEqual(counts["accepted"], 1)
		self.assertEqual(counts["rejected"], 1)
		self.assertTrue((base / "processed" / "evt-5.json").exists())
		self.assertTrue((base / "rejected" / "junk.json").exists())
		for sub in ("processed", "rejected"):
			for path in (base / sub).iterdir():
				path.unlink()


class TestInboundSecretRotation(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user("edi.rotation@hinode.example", ["System Manager"])
		make_user("rotation.clerk@hinode.example", ["Purchase User"])
		frappe.get_doc(
			{
				"doctype": "Integration Partner",
				"partner_code": "TEST-ROTATE",
				"partner_name": "Test Rotation",
				"partner_type": "Supplier",
				"adapter": "HTTPS Webhook",
				"message_format": "JSON",
				"allowed_message_types": "asn",
				"service_user": "edi.rotation@hinode.example",
				"key_id": "k-rotate",
				"inbound_secret": "first-secret-0123456789",
			}
		).insert(ignore_permissions=True)

	def delivery(self, secret: str) -> tuple[str, bytes]:
		body = envelope("ROT-1", message_type="asn")
		return signing.build_header("k-rotate", secret, body), body

	def verify(self, delivery: tuple[str, bytes]) -> None:
		inbound.verify_delivery(frappe.get_doc("Integration Partner", "TEST-ROTATE"), *delivery)

	def test_previous_secret_is_accepted_for_the_overlap_only(self):
		from automotive_core.integration_hub.partners import MAX_OVERLAP_HOURS, rotate_inbound_secret

		old = self.delivery("first-secret-0123456789")
		result = rotate_inbound_secret("TEST-ROTATE", overlap_hours=2)
		self.assertNotEqual(result["secret"], "first-secret-0123456789")
		self.verify(self.delivery(result["secret"]))
		self.verify(old)  # the partner has not switched yet
		self.assertTrue(
			frappe.db.exists(
				"Audit Event",
				{"event_type": "integration.inbound_secret_rotated", "reference_name": "TEST-ROTATE"},
			)
		)
		frappe.db.set_value(
			"Integration Partner",
			"TEST-ROTATE",
			"previous_inbound_secret_until",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-1),
		)
		with self.assertRaises(frappe.AuthenticationError):
			self.verify(old)  # the overlap is over
		self.verify(self.delivery(result["secret"]))
		# no overlap: the secret it replaces stops at once
		second = rotate_inbound_secret("TEST-ROTATE", overlap_hours=0)
		with self.assertRaises(frappe.AuthenticationError):
			self.verify(self.delivery(result["secret"]))
		self.verify(self.delivery(second["secret"]))
		with self.assertRaises(frappe.ValidationError):
			rotate_inbound_secret("TEST-ROTATE", overlap_hours=MAX_OVERLAP_HOURS + 1)
		with self.set_user("rotation.clerk@hinode.example"), self.assertRaises(frappe.PermissionError):
			rotate_inbound_secret("TEST-ROTATE")


GUARD = "TEST-GUARD"
GUARD_SECRET = "guard-secret-0123456789"


class TestInboundDeliveryGuards(IntegrationTestCase):
	"""The signed HTTPS endpoint: a captured delivery sent again is refused, and a partner that
	sends too much in a minute is throttled (ERP-11)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user("edi.guard@hinode.example", ["System Manager"])
		for code in (GUARD, f"{GUARD}-2"):
			frappe.get_doc(
				{
					"doctype": "Integration Partner",
					"partner_code": code,
					"partner_name": code,
					"partner_type": "Supplier",
					"adapter": "HTTPS Webhook",
					"message_format": "JSON",
					"allowed_message_types": "production_event",
					"service_user": "edi.guard@hinode.example",
					"key_id": "k-guard",
					"inbound_secret": GUARD_SECRET,
				}
			).insert(ignore_permissions=True)

	def setUp(self):
		frappe.flags.automotive_pending_inbound = []
		self._request = getattr(frappe.local, "request", None)

	def tearDown(self):
		frappe.local.request = self._request
		frappe.local.response = frappe._dict({"docs": []})

	def post(self, body: bytes, signature: str, partner: str = GUARD) -> dict:
		"""The endpoint on a request as the partner's system sends it (no HTTP server)."""
		frappe.local.request = Request(
			EnvironBuilder(
				path="/api/method/automotive_core.integration_hub.inbound.receive",
				method="POST",
				data=body,
				headers={
					signing.PARTNER_HEADER: partner,
					signing.HEADER: signature,
					"Content-Type": "application/json",
				},
			).get_environ()
		)
		return inbound.receive()

	def test_a_replayed_delivery_is_refused_and_stores_nothing(self):
		body = envelope("GUARD-1")
		signature = signing.build_header("k-guard", GUARD_SECRET, body)
		first = self.post(body, signature)
		self.assertEqual((first["status"], frappe.local.response.http_status_code), ("accepted", 202))
		# the same request captured and sent again: the signature is still valid, its nonce is spent
		with self.assertRaises(frappe.AuthenticationError):
			self.post(body, signature)
		self.assertEqual(frappe.db.count("Integration Inbound Message", {"partner": GUARD}), 1)
		self.assertEqual(frappe.db.count("Integration Nonce", {"partner": GUARD}), 1)
		# the partner's own retry is signed afresh and answered with the stored outcome
		again = self.post(body, signing.build_header("k-guard", GUARD_SECRET, body))
		self.assertEqual((again["status"], again["inbound_id"]), ("duplicate", first["inbound_id"]))
		self.assertEqual(frappe.local.response.http_status_code, 200)
		self.assertEqual(frappe.db.count("Integration Inbound Message", {"partner": GUARD}), 1)
		# nonces are remembered per partner: another partner's first use of the value is fine
		nonce = signing.parse_header(signature).nonce
		inbound._remember_nonce(f"{GUARD}-2", nonce)
		with self.assertRaises(frappe.AuthenticationError):
			inbound._remember_nonce(f"{GUARD}-2", nonce)

	def test_a_partner_over_its_rate_limit_is_refused_until_the_next_minute(self):
		previous = frappe.db.get_single_value("Automotive Settings", "inbound_rate_limit_per_minute")
		frappe.db.set_single_value("Automotive Settings", "inbound_rate_limit_per_minute", 3)
		self.addCleanup(
			frappe.db.set_single_value, "Automotive Settings", "inbound_rate_limit_per_minute", previous
		)
		minute = datetime.datetime(2031, 3, 4, 5, 6, 7)
		later = minute + datetime.timedelta(minutes=1)
		# the counters live in Redis (not rolled back with the test): keys of minutes no other test uses
		keys = [
			frappe.cache.make_key(f"automotive:inbound-rate:{partner}:{at:%Y%m%d%H%M}")
			for partner in (GUARD, f"{GUARD}-2")
			for at in (minute, later)
		]
		frappe.cache.delete(*keys)
		self.addCleanup(frappe.cache.delete, *keys)
		with mock.patch.object(inbound, "now_datetime", return_value=minute):
			for _ in range(3):
				inbound._rate_limit(GUARD)
			with self.assertRaises(frappe.TooManyRequestsError):
				inbound._rate_limit(GUARD)
			self.assertEqual(frappe.local.response.http_status_code, 429)
			inbound._rate_limit(f"{GUARD}-2")  # every partner has its own budget
		with mock.patch.object(inbound, "now_datetime", return_value=later):
			inbound._rate_limit(GUARD)  # a new minute, a new budget


def todo(label: str) -> str:
	return frappe.get_doc({"doctype": "ToDo", "description": label}).insert().name


class TestOutbox(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for code, adapter, url in (
			("TEST-DROP", "Local File Drop", None),
			("TEST-HTTPS", "HTTPS Webhook", "https://partner.example/hooks"),
		):
			frappe.get_doc(
				{
					"doctype": "Integration Partner",
					"partner_code": code,
					"partner_name": code,
					"partner_type": "Supplier",
					"adapter": adapter,
					"endpoint_url": url,
					"outbound_secret": "out-secret" if url else None,
					"key_id": "k-1",
					"subscribed_events": "purchase_order.submitted",
				}
			).insert(ignore_permissions=True)

	def test_publish_is_idempotent_and_file_drop_delivers(self):
		ref1 = todo("po 1")
		first = outbox.publish(
			"purchase_order.submitted", {"po": "PO-1"}, "ToDo", ref1, partners=["TEST-DROP"]
		)
		again = outbox.publish(
			"purchase_order.submitted", {"po": "PO-1"}, "ToDo", ref1, partners=["TEST-DROP"]
		)
		self.assertEqual(len(first), 1)
		self.assertEqual(again, [])
		self.assertEqual(outbox.deliver(first[0]), "Delivered")
		written = Path(partner_dir("TEST-DROP")) / "outbox" / f"{first[0]}.json"
		self.assertEqual(json.loads(written.read_text())["data"], {"po": "PO-1"})
		written.unlink()
		self.assertEqual(
			frappe.db.get_value("Integration Partner", "TEST-DROP", "connection_status"), "Local Adapter Only"
		)

	def test_https_requires_allowlist_then_signs_and_retries(self):
		ref2 = todo("po 2")
		[name] = outbox.publish(
			"purchase_order.submitted", {"po": "PO-2"}, "ToDo", ref2, partners=["TEST-HTTPS"]
		)
		self.assertEqual(outbox.deliver(name), "Failed")  # host not allowlisted
		self.assertIn("allowlist", frappe.db.get_value("Integration Outbound Message", name, "last_error"))
		frappe.db.set_single_value("Automotive Settings", "outbound_host_allowlist", "partner.example")
		frappe.clear_document_cache("Automotive Settings", "Automotive Settings")
		captured = {}

		def fake_post(url, data, headers, timeout, allow_redirects):
			captured.update(url=url, data=data, headers=headers, allow_redirects=allow_redirects)
			response = mock.Mock(status_code=503, text="busy")
			return response

		with mock.patch(
			"automotive_core.integration_hub.adapters.https.requests.post", side_effect=fake_post
		):
			self.assertEqual(outbox.deliver(name), "Failed")
		sig = signing.verify(captured["headers"][signing.HEADER], captured["data"], "out-secret", 300)
		self.assertEqual(sig.key_id, "k-1")
		self.assertFalse(captured["allow_redirects"])
		self.assertEqual(
			captured["headers"]["Idempotency-Key"], f"purchase_order.submitted:ToDo:{ref2}:TEST-HTTPS"
		)
		with mock.patch(
			"automotive_core.integration_hub.adapters.https.requests.post",
			return_value=mock.Mock(status_code=202, text="ok"),
		):
			self.assertEqual(outbox.deliver(name), "Delivered")
		msg = frappe.get_doc("Integration Outbound Message", name)
		self.assertEqual(
			[a.outcome for a in msg.delivery_attempts],
			["Retryable Failure", "Retryable Failure", "Delivered"],
		)
		self.assertEqual(
			frappe.db.get_value("Integration Partner", "TEST-HTTPS", "connection_status"), "Live"
		)

	def test_permanent_failure_dead_letters(self):
		ref3 = todo("po 3")
		[name] = outbox.publish(
			"purchase_order.submitted", {"po": "PO-3"}, "ToDo", ref3, partners=["TEST-HTTPS"]
		)
		frappe.db.set_single_value("Automotive Settings", "outbound_host_allowlist", "partner.example")
		frappe.clear_document_cache("Automotive Settings", "Automotive Settings")
		with mock.patch(
			"automotive_core.integration_hub.adapters.https.requests.post",
			return_value=mock.Mock(status_code=400, text="bad"),
		):
			self.assertEqual(outbox.deliver(name), "Dead Letter")
		self.assertEqual(outbox.classify(DeliveryResult(ok=False, status_code=429)), "Retryable Failure")
