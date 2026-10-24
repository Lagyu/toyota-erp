"""Privileged MFA: OTP for the privileged roles only (setup/roles.py)."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.twofactor import two_factor_is_enabled_for_

from automotive_core.setup.roles import PRIVILEGED_ROLES, two_factor_for_privileged_roles
from automotive_core.tests.utils import make_user


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
