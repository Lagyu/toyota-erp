"""Privileged MFA: OTP for the privileged roles only (setup/roles.py), the authenticator
enrolment of a privileged user, and account recovery by resetting the OTP secret."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.twofactor import two_factor_is_enabled_for_

from automotive_core.setup.roles import PRIVILEGED_ROLES, two_factor_for_privileged_roles
from automotive_core.tests.utils import as_user, make_user


class Login:
	"""The parts of frappe.auth.LoginManager the OTP check uses."""

	def __init__(self, user: str):
		self.user = user

	def fail(self, message, user=None):
		raise frappe.AuthenticationError(message)


class TestPrivilegedTwoFactor(IntegrationTestCase):
	def test_enabling_two_factor_does_not_reach_every_user(self):
		# what turning 2FA on in System Settings does to Frappe's "All" role
		frappe.db.set_value("Role", "All", "two_factor_auth", 1, update_modified=False)
		two_factor_for_privileged_roles()
		self.assertEqual(frappe.db.get_value("Role", "All", "two_factor_auth"), 0)
		for role in PRIVILEGED_ROLES:
			if frappe.db.exists("Role", role):
				self.assertEqual(frappe.db.get_value("Role", role, "two_factor_auth"), 1, role)
		clerk = make_user("two-factor.clerk@hinode.example", ["Stock User"])
		controller = make_user("two-factor.controller@hinode.example", ["Group Controller"])
		self.assertFalse(two_factor_is_enabled_for_(clerk))
		self.assertTrue(two_factor_is_enabled_for_(controller))

	def test_an_authenticator_code_completes_enrolment_and_a_reset_starts_it_again(self):
		"""Method OTP App: at a privileged user's first sign-in Frappe makes the OTP secret (the QR
		code it e-mails); the authenticator's code completes the enrolment. Account recovery
		(security-operations.md): a System Manager's Reset OTP Secret discards it, and the next
		sign-in enrols a new authenticator."""
		import pyotp
		from frappe.twofactor import (
			cache_2fa_data,
			confirm_otp_token,
			get_default,
			get_otpsecret_for_,
			reset_otp_secret,
		)

		settings = ("enable_two_factor_auth", "two_factor_method")
		previous = dict(zip(settings, frappe.db.get_value("System Settings", None, settings), strict=True))

		def restore():  # Frappe commits when it makes a secret: put the settings back for good
			frappe.db.set_single_value("System Settings", previous)
			frappe.db.commit()

		self.addCleanup(restore)
		frappe.db.set_single_value(
			"System Settings", {"enable_two_factor_auth": 1, "two_factor_method": "OTP App"}
		)
		two_factor_for_privileged_roles()
		clerk = make_user("otp.clerk@hinode.example", ["Stock User"])
		controller = make_user("otp.controller@hinode.example", ["Group Controller"])

		self.assertTrue(confirm_otp_token(Login(clerk)))  # no code asked of an unprivileged user
		self.assertFalse(confirm_otp_token(Login(controller)))  # a privileged one cannot skip it
		secret = get_otpsecret_for_(controller)
		self.assertIsNone(get_default(f"{controller}_otplogin"))  # not enrolled: the QR code is offered
		tmp_id = frappe.generate_hash(length=8)
		cache_2fa_data(controller, None, secret, tmp_id)
		code = pyotp.TOTP(secret).now()
		with self.assertRaises(frappe.AuthenticationError):
			confirm_otp_token(Login(controller), otp=f"{(int(code) + 1) % 1_000_000:06d}", tmp_id=tmp_id)
		self.assertTrue(confirm_otp_token(Login(controller), otp=pyotp.TOTP(secret).now(), tmp_id=tmp_id))
		self.assertTrue(get_default(f"{controller}_otplogin"))  # enrolled: no QR code any more

		# account recovery: only a System Manager may reset another user's secret
		with as_user(clerk), self.assertRaises(frappe.PermissionError):
			reset_otp_secret(controller)
		reset_otp_secret(controller)
		self.assertIsNone(get_default(f"{controller}_otplogin"))
		self.assertNotEqual(get_otpsecret_for_(controller), secret)  # a new enrolment, a new secret
