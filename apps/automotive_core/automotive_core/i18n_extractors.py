"""Babel extractors for translatable strings Frappe's own extractors do not read
(registered per app in babel_extractors.csv)."""

from __future__ import annotations

import io
import json


def print_format(fileobj, keywords, comment_tags, options):
	"""A standard Print Format's JSON: its name (the print view lists it) and the ``_()``
	strings of the Jinja template in its ``html`` field (line numbers within the template)."""
	from frappe.gettext.extractors.html_template import extract as extract_template

	data = json.load(fileobj)
	if data.get("name"):
		yield 1, "_", data["name"], ["Print Format name"]
	html = data.get("html") or ""
	if html:
		yield from extract_template(io.BytesIO(html.encode()), keywords, comment_tags, options)
