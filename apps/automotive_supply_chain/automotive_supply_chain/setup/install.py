"""Install / migrate entry point for automotive_supply_chain (idempotent)."""

from __future__ import annotations

import frappe

from automotive_supply_chain.setup import custom_fields


def after_install() -> None:
	"""A new site installs the apps one after another: automotive_core's setup (permissions,
	report roles, the KPI catalogue, database guards) ran before this app's DocTypes, reports
	and hooks existed, so it runs again now that they do. A migration runs it once every app's
	DocTypes are synced."""
	from automotive_core.setup.install import after_migrate as core_setup

	after_migrate()
	core_setup()


def after_migrate() -> None:
	custom_fields.apply()
	frappe.clear_cache()
