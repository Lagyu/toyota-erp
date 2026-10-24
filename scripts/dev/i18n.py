#!/usr/bin/env python3
"""Translation catalogues of the custom apps (ERP-12 English/Japanese).

Runs with the bench's Python inside the development container:

	cd ~/dev/frappe-bench/sites && ../env/bin/python /workspace/scripts/dev/i18n.py pot
	... i18n.py po ja          # create or update locale/ja.po from the template
	... i18n.py stats ja       # translated / total per app

``pot`` is Frappe's own extraction (``frappe.gettext.translate.generate_pot``: the same
Babel extractors for Python, JavaScript, DocType/workspace JSON and templates) with a plain
directory filter: Frappe's filter asks git about ignored paths and fails for apps linked
into the bench from outside it, as in this repository's development stack.
"""

from __future__ import annotations

import os
import sys

APPS = ("automotive_core", "automotive_manufacturing", "automotive_supply_chain", "automotive_portals")
SKIP = {"public/dist", "node_modules", "__pycache__"}


def _init() -> None:
	import frappe

	frappe.init(site=os.environ.get("SITE", "test.localhost"), sites_path=".")


def pot() -> None:
	from babel.messages.extract import extract_from_dir
	from frappe.gettext.translate import (
		PYTHON_KEYWORDS,
		_get_ignored_strings,
		get_method_map,
		new_catalog,
		write_catalog,
	)
	from frappe.utils import get_bench_path

	def directory_filter(dirpath) -> bool:
		path = str(dirpath)
		name = os.path.basename(path)
		return not (name.startswith((".", "_")) or any(skip in path for skip in SKIP))

	default_map = get_method_map("frappe")
	for app in APPS:
		catalog = new_catalog(app)
		ignored = _get_ignored_strings(app)
		# walk the app through its link in the bench: extractors expect paths inside the bench
		for filename, lineno, message, comments, context in extract_from_dir(
			os.path.join(get_bench_path(), "apps", app), get_method_map(app) + default_map,
			directory_filter=directory_filter, keywords=PYTHON_KEYWORDS,
		):
			if message and (message, context) not in ignored:
				catalog.add(message, locations=[(filename, lineno)], auto_comments=comments, context=context)
		print(f"{app}: {len(catalog)} messages -> {write_catalog(app, catalog)}")


def po(locale: str) -> None:
	from frappe.gettext.translate import get_po_path, new_po, update_po

	for app in APPS:
		if get_po_path(app, locale).exists():
			update_po(app, locale)
		else:
			new_po(locale, app)


def stats(locale: str) -> None:
	from frappe.gettext.translate import get_catalog

	for app in APPS:
		catalog = get_catalog(app, locale)
		total = sum(1 for m in catalog if m.id)
		done = sum(1 for m in catalog if m.id and m.string)
		print(f"{app}: {done}/{total} translated")


if __name__ == "__main__":
	_init()
	command, *args = sys.argv[1:] or ["pot"]
	{"pot": pot, "po": po, "stats": stats}[command](*args)
