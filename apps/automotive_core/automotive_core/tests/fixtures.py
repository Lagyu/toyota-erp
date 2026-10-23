"""`before_tests` hook: prepare the test site once (committed), then each test class
works inside its own transaction that IntegrationTestCase rolls back."""

from __future__ import annotations

import frappe


def before_tests() -> None:
	frappe.clear_cache()
	frappe.set_user("Administrator")
	from automotive_core.setup.tenant import apply
	from automotive_core.tests.utils import ensure_test_doctypes

	apply("hinode")
	ensure_test_doctypes()
	frappe.db.commit()
	frappe.clear_cache()
