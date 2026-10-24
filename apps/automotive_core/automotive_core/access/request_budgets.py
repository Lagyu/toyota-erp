"""Per-user request limits for the partner portals and the shop floor (SEC-5).

Frappe's own limiter (``frappe.rate_limiter.rate_limit``) counts per IP address, and
partners reach the portals through their companies' proxies: one supplier's busy office
would use up the budget of everyone behind the same address, and the tablets of a line
share one. These limits count per signed-in user and per group of methods, in one-minute
windows kept in the site's Redis cache (shared by all web workers). A request over the
limit is refused with HTTP 429 before the method runs. The limits are in Automotive
Settings (Request Limits). Desk work is not limited here; the inbound partner endpoint has
its own limit per partner (integration_hub.inbound).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

# group -> (Automotive Settings field, default per user and minute)
LIMITS = {
	"portal-read": ("portal_reads_per_minute", 300),
	"portal-change": ("portal_changes_per_minute", 60),
	"shop-floor": ("shop_floor_requests_per_minute", 240),
}
_PORTAL = ("automotive_portals.api.",)
_SHOP_FLOOR = ("automotive_manufacturing.mes.", "automotive_manufacturing.shop_floor.")
_METHOD_PATHS = ("/api/method/", "/api/v1/method/", "/api/v2/method/")
_READS = ("GET", "HEAD", "OPTIONS")


def group_of(method: str, http_method: str, user: str) -> str | None:
	"""The limit a call of ``method`` counts against, if any."""
	if method.startswith(_PORTAL):
		return "portal-read" if http_method.upper() in _READS else "portal-change"
	if method.startswith(_SHOP_FLOOR):
		return "shop-floor"
	# documents partners attach (shipment papers) arrive through Frappe's own upload method
	if method == "upload_file" and frappe.db.get_value("User", user, "user_type") == "Website User":
		return "portal-change"
	return None


def before_request() -> None:
	request = getattr(frappe.local, "request", None)
	if request is None:
		return
	path = request.path or ""
	method = next((path[len(p) :].strip("/") for p in _METHOD_PATHS if path.startswith(p)), None)
	method = method or frappe.form_dict.get("cmd")  # the deprecated RPC form
	if not method:
		return
	user = frappe.session.user
	group = group_of(method, request.method or "GET", user)
	if group:
		spend(group, user)


def limit(group: str) -> int:
	field, default = LIMITS[group]
	return cint(frappe.get_cached_doc("Automotive Settings").get(field)) or default


def spend(group: str, user: str) -> int:
	"""Count one request of ``group`` for ``user`` in the current minute; refuse it (HTTP
	429) over the limit. Guests count per address."""
	who = user if user != "Guest" else f"guest:{frappe.local.request_ip}"
	key = frappe.cache.make_key(f"automotive:budget:{group}:{who}:{now_datetime():%Y%m%d%H%M}")
	count = frappe.cache.incrby(key, 1)
	if count == 1:
		frappe.cache.expire(key, 120)
	if count > limit(group):
		raise frappe.TooManyRequestsError(_("Too many requests. Please wait a minute and try again."))
	return count
