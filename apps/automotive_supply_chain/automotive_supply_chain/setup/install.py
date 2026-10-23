"""Install / migrate entry point for automotive_supply_chain (idempotent)."""

from __future__ import annotations

import frappe

from automotive_supply_chain.setup import custom_fields


def after_migrate() -> None:
	custom_fields.apply()
	frappe.clear_cache()
