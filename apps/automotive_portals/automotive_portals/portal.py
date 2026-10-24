"""Shared page context for the supplier and dealer portals (ERP-05, ERP-08, ERP-12).

Pages are thin shells: everything they show comes from the party-scoped APIs in
``automotive_portals.api``, which resolve the caller's organisation on the server. The
page itself only checks that the visitor is a signed-in partner user of the right kind
and hands the browser its translated UI strings.
"""

from __future__ import annotations

import functools
from pathlib import Path
from urllib.parse import quote

import frappe
from frappe import _

from automotive_supply_chain.partners import capabilities, party_of_user

PARTY_NAME_FIELD = {"Supplier": "supplier_name", "Customer": "customer_name"}
BUNDLE = Path(__file__).parent / "public" / "js" / "portal"


def portal_context(context, party_type: str, title: str) -> None:
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=" + quote(frappe.local.request.path)
		raise frappe.Redirect(302)  # temporary: a permanent redirect would be cached by the browser
	mine = party_of_user()
	if not mine or mine[0] != party_type:
		raise frappe.PermissionError(_("This portal is only available to {0} users.").format(
			_("supplier") if party_type == "Supplier" else _("dealer")))
	context.no_cache = 1
	context.show_sidebar = False
	context.full_width = True
	context.title = title
	context.party = mine[1]
	context.party_name = frappe.db.get_value(party_type, mine[1], PARTY_NAME_FIELD[party_type]) or mine[1]
	context.capabilities = frappe.as_json(capabilities(party_type), indent=None)
	context.translated_messages = script_json(_messages(frappe.local.lang))


def script_json(value) -> str:
	"""JSON that is safe inside an inline <script> (no tag or entity can be closed early)."""
	return (frappe.as_json(value, indent=None).replace("<", "\\u003c").replace(">", "\\u003e")
		.replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


@functools.cache
def _sources() -> tuple[str, ...]:
	return tuple(sorted(str(p) for p in BUNDLE.glob("*.js")))


def _messages(lang: str) -> dict[str, str]:
	"""Translations for every ``__("...")`` string in the portal scripts."""
	cache_key = f"automotive_portals:messages:{lang}"
	cached = frappe.cache.get_value(cache_key)
	if cached is not None:
		return cached
	from frappe.translate import extract_messages_from_javascript_code

	out: dict[str, str] = {}
	for path in _sources():
		code = Path(path).read_text(encoding="utf-8")
		for _line, message, msg_context in extract_messages_from_javascript_code(code):
			key = f"{message}:{msg_context}" if msg_context else message
			out[key] = _(message, context=msg_context)
	frappe.cache.set_value(cache_key, out, expires_in_sec=3600)
	return out


def home_page(user: str) -> str | None:
	"""Home page after login: the partner's portal for partner users, the default otherwise."""
	if frappe.db.get_value("User", user, "user_type") != "Website User":
		return None
	mine = party_of_user(user)
	if not mine:
		return None
	return "supplier-portal" if mine[0] == "Supplier" else "dealer-portal"
