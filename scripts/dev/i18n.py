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
_DOCUMENTS = "|".join((
	"sales_invoice", "purchase_invoice", "payment_entry", "journal_entry", "sales_order", "purchase_order",
	"customer", "supplier", "item", "stock_entry", "delivery_note", "purchase_receipt", "batch", "serial_no",
	"warehouse", "stock_reconciliation", "quality_inspection", "bom", "work_order", "job_card",
	"production_plan", "workstation", "operation", "routing", "material_request", "blanket_order",
	"supplier_quotation", "request_for_quotation", "stock_reservation_entry", "serial_and_batch_bundle",
	"landed_cost_voucher", "bank_reconciliation_tool", "bank_transaction", "company", "account",
	"cost_center", "fiscal_year", "tax_rule", "sales_taxes_and_charges_template",
	"purchase_taxes_and_charges_template", "price_list", "item_price", "pricing_rule", "payment_terms_template",
	"quality_action", "non_conformance", "employee", "holiday_list", "uom", "item_group", "customer_group",
	"supplier_group", "territory", "vehicle",
))
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
		if app == "automotive_core":
			_add_upstream(catalog)
		print(f"{app}: {len(catalog)} messages -> {write_catalog(app, catalog)}")


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
				catalog.add(message.id, locations=message.locations, auto_comments=message.auto_comments,
					context=message.context, flags=message.flags)
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
	{"pot": pot, "po": po, "stats": stats}[command](*args)
