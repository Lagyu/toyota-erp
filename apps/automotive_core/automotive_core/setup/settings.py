"""Default values for singles that Frappe does not fill from JSON defaults on install."""

from __future__ import annotations

import frappe


def ensure_defaults() -> None:
	settings = frappe.get_single("Automotive Settings")
	changed = False
	for field in settings.meta.fields:
		if field.default and settings.get(field.fieldname) in (None, ""):
			settings.set(field.fieldname, field.default)
			changed = True
	if changed:
		settings.flags.ignore_permissions = True
		settings.save()
