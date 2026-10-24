#!/usr/bin/env python3
"""Translation catalogues of the custom apps (ERP-12 English/Japanese).

Runs with the bench's Python inside the development container:

	cd ~/dev/frappe-bench/sites && ../env/bin/python /workspace/scripts/dev/i18n.py pot
	... i18n.py po ja          # create or update locale/ja.po from the template
	... i18n.py stats ja       # translated / total per app
	... i18n.py fill ja        # untranslated entries from the other apps' catalogues
	... i18n.py check ja       # every string in the code has a translation (CI)

scripts/dev/i18n-host.sh runs the same commands without the containers.

``pot`` is Frappe's own extraction (``frappe.gettext.translate.generate_pot``: the same
Babel extractors for Python, JavaScript, DocType/workspace JSON and templates) with a plain
directory filter: Frappe's filter asks git about ignored paths and fails for apps linked
into the bench from outside it, as in this repository's development stack.

Frappe and ERPNext ship no Japanese catalogue. Frappe merges every app's catalogue into one
dictionary per language, so the core app's template also carries the upstream strings our
users meet most (UPSTREAM: the desk itself, the login pages and the standard documents of
the reference processes), and its ja.po translates them for the whole site. Everything else
upstream stays English.
"""

from __future__ import annotations

import os
import re
import sys

APPS = ("automotive_core", "automotive_manufacturing", "automotive_supply_chain", "automotive_portals")
SKIP = {"public/dist", "node_modules", "__pycache__"}

# ERPNext doctypes of the reference processes (ERP-01 ... ERP-09)
_DOCUMENTS = "|".join(
	(
		"sales_invoice",
		"purchase_invoice",
		"payment_entry",
		"journal_entry",
		"sales_order",
		"purchase_order",
		"customer",
		"supplier",
		"item",
		"stock_entry",
		"delivery_note",
		"purchase_receipt",
		"batch",
		"serial_no",
		"warehouse",
		"stock_reconciliation",
		"quality_inspection",
		"bom",
		"work_order",
		"job_card",
		"production_plan",
		"workstation",
		"operation",
		"routing",
		"material_request",
		"blanket_order",
		"supplier_quotation",
		"request_for_quotation",
		"stock_reservation_entry",
		"serial_and_batch_bundle",
		"landed_cost_voucher",
		"bank_reconciliation_tool",
		"bank_transaction",
		"company",
		"account",
		"cost_center",
		"fiscal_year",
		"tax_rule",
		"sales_taxes_and_charges_template",
		"purchase_taxes_and_charges_template",
		"price_list",
		"item_price",
		"pricing_rule",
		"payment_terms_template",
		"quality_action",
		"non_conformance",
		"employee",
		"holiday_list",
		"uom",
		"item_group",
		"customer_group",
		"supplier_group",
		"territory",
		"vehicle",
	)
)
# upstream app -> source locations (as written in its locale/main.pot) whose strings core translates
UPSTREAM = {
	"frappe": (
		r"^frappe/public/js/frappe/(form|list|views|ui|widgets|model|utils|desk|router|data_import)/",
		r"^frappe/public/js/frappe/[^/]+\.js$",
		r"^frappe/core/doctype/(user|role|file|comment|communication|docshare|user_permission|version|"
		r"data_import)/",
		r"^frappe/desk/doctype/(todo|workspace|dashboard|number_card|notification_log|kanban_board)/",
		r"^frappe/(model|desk)/[a-z_]+\.py$",
		r"^frappe/www/(login|update-password|me)\b",
		r"^frappe/templates/includes/login/",
	),
	"erpnext": (
		rf"^erpnext/[a-z_]+/doctype/({_DOCUMENTS})/",
		r"^erpnext/public/js/(controllers|utils)/",
		r"^erpnext/public/js/[a-z_]+\.js$",
		r"^erpnext/controllers/",
	),
}


def _init() -> None:
	import frappe

	frappe.init(site=os.environ.get("SITE", "test.localhost"), sites_path=".")


def _extract(app: str):
	"""The app's message catalogue from its source (and, for the core app, the upstream
	strings it translates for the whole site)."""
	from babel.messages.extract import extract_from_dir
	from frappe.gettext.translate import PYTHON_KEYWORDS, _get_ignored_strings, get_method_map, new_catalog
	from frappe.utils import get_bench_path

	def directory_filter(dirpath) -> bool:
		path = str(dirpath)
		name = os.path.basename(path)
		return not (name.startswith((".", "_")) or any(skip in path for skip in SKIP))

	catalog = new_catalog(app)
	ignored = _get_ignored_strings(app)
	# the app's directory in the bench (a link to this repository), or I18N_APPS_DIR (the
	# repository's apps/ when the bench's links do not resolve, as on the host)
	apps_dir = os.environ.get("I18N_APPS_DIR") or os.path.join(get_bench_path(), "apps")
	for filename, lineno, message, comments, context in extract_from_dir(
		os.path.join(apps_dir, app),
		get_method_map(app) + get_method_map("frappe"),
		directory_filter=directory_filter,
		keywords=PYTHON_KEYWORDS,
	):
		if message and (message, context) not in ignored:
			catalog.add(message, locations=[(filename, lineno)], auto_comments=comments, context=context)
	if app == "automotive_core":
		_add_upstream(catalog)
	return catalog


def pot() -> None:
	from frappe.gettext.translate import write_catalog

	for app in APPS:
		catalog = _extract(app)
		print(f"{app}: {len(catalog)} messages -> {write_catalog(app, catalog)}")


def _translations(locale: str) -> dict:
	"""(msgid, context) -> translation across the custom apps' catalogues: Frappe merges
	every app's catalogue into one dictionary per language."""
	from frappe.gettext.translate import get_catalog

	known = {}
	for app in APPS:
		for message in get_catalog(app, locale):
			if (
				message.id
				and message.string
				and (not isinstance(message.string, tuple) or all(message.string))
			):
				known.setdefault((message.id, message.context), message.string)
	return known


def check(locale: str) -> None:
	"""Fail when a string in the code has no translation in any of the catalogues: the
	catalogue checks in lint.sh only see strings someone has already extracted."""
	known = _translations(locale)
	missing = 0
	for app in APPS:
		for message in _extract(app):
			# a string with a context falls back to its plain translation, as Frappe does
			if not message.id or (message.id, message.context) in known or (message.id, None) in known:
				continue
			missing += 1
			where = ", ".join(f"{path}:{line}" for path, line in message.locations[:2])
			print(
				f"{app}: untranslated {message.id!r}"
				+ (f" [{message.context}]" if message.context else "")
				+ f" ({where})"
			)
	if missing:
		print(
			f"{missing} string(s) without a {locale} translation: i18n.py pot, po {locale}, fill {locale}, then translate"
		)
		sys.exit(1)
	print(f"{locale}: every extracted string is translated")


def fill(locale: str) -> None:
	"""Fill each app's untranslated entries from the other catalogues (one translation per
	string, as the glossary requires); what remains needs a translator."""
	from frappe.gettext.translate import get_catalog, write_catalog

	known = _translations(locale)
	for app in APPS:
		catalog = get_catalog(app, locale)
		filled = left = 0
		for message in catalog:
			if not message.id or message.string:
				continue
			value = known.get((message.id, message.context))
			if value is None and message.context:
				value = known.get((message.id, None))
			if value is None:
				left += 1
				continue
			message.string = value
			message.flags.discard("fuzzy")
			filled += 1
		write_catalog(app, catalog, locale)
		print(f"{app}: filled {filled}, {left} to translate")


def _add_upstream(catalog) -> None:
	import frappe
	from babel.messages.pofile import read_po

	for app, patterns in UPSTREAM.items():
		wanted = [re.compile(p) for p in patterns]
		added = 0
		with open(frappe.get_app_path(app, "locale", "main.pot"), "rb") as handle:
			upstream = read_po(handle)
		for message in upstream:
			if not message.id or catalog.get(message.id, message.context) is not None:
				continue
			if any(w.search(path) for path, _line in message.locations for w in wanted):
				catalog.add(
					message.id,
					locations=message.locations,
					auto_comments=message.auto_comments,
					context=message.context,
					flags=message.flags,
				)
				added += 1
		print(f"automotive_core: {added} {app} messages")


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
	{"pot": pot, "po": po, "stats": stats, "fill": fill, "check": check}[command](*args)
