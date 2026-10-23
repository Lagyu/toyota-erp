"""Install / migrate entry points for automotive_core (idempotent)."""

from __future__ import annotations

import frappe

from automotive_core.setup import custom_fields, db_guards, permissions, roles, settings


def after_install() -> None:
	after_migrate()


def after_migrate() -> None:
	roles.ensure_roles()
	roles.ensure_role_profiles()
	custom_fields.apply()
	permissions.apply()
	settings.ensure_defaults()
	db_guards.apply()
	from automotive_core.analytics import catalog

	catalog.sync()
	frappe.clear_cache()


def before_uninstall() -> None:
	db_guards.remove()
