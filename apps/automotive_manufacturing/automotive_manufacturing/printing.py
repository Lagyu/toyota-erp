"""Jinja helpers for print formats, registered through ``jinja`` in hooks.py."""

from __future__ import annotations


def automotive_qr_code(value: str) -> str:
	"""QR code of ``value`` as an SVG data URI for an ``<img>`` (Frappe's own Barcode helper).

	Empty when that helper is unavailable, so a print format falls back to the text."""
	try:
		from frappe.utils.print_format_generator import get_qr_code
	except ImportError:
		return ""
	return get_qr_code(str(value))
