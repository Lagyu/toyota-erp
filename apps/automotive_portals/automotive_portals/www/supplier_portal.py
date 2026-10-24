import frappe
from frappe import _

from automotive_portals.portal import portal_context

no_cache = 1


def get_context(context):
	portal_context(context, "Supplier", _("Supplier Portal"))
