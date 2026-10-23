"""BOM revisions, effectivity and protection of production history (ERP-02, J-04).

* Every released BOM has a revision letter and an effectivity window. A new
  revision supersedes the previous one from its effective date (set by an ECO).
* A Work Order uses the BOM effective at its planned start date unless the planner
  pins one explicitly; a submitted Work Order keeps its BOM forever (ERPNext makes
  submitted documents immutable), so history stays pinned to the approved revision.
* Released BOMs cannot be re-pointed silently: ERPNext's BOM Update Tool is limited
  to ECO implementation, effectivity fields change only through an ECO, and a BOM
  used by any Work Order cannot be cancelled.
"""

from __future__ import annotations

import string

import frappe
from frappe import _
from frappe.utils import add_days, flt, getdate, nowdate

from automotive_core.utils import commit


class EngineeringControlError(frappe.ValidationError):
	pass


def next_revision(existing: list[str]) -> str:
	"""A, B, ... Z, AA, AB, ... (skipping I and O, which read like digits)."""
	letters = [c for c in string.ascii_uppercase if c not in "IO"]

	def to_int(rev: str) -> int:
		value = 0
		for char in rev:
			value = value * len(letters) + letters.index(char) + 1
		return value

	def to_rev(value: int) -> str:
		out = ""
		while value:
			value, rem = divmod(value - 1, len(letters))
			out = letters[rem] + out
		return out

	valid = [r for r in existing if r and all(c in letters for c in r)]
	return to_rev(max((to_int(r) for r in valid), default=0) + 1)


def item_revisions(item_code: str, exclude: str | None = None) -> list[str]:
	filters = {"item": item_code, "docstatus": ("<", 2)}
	if exclude:
		filters["name"] = ("!=", exclude)
	return frappe.get_all("BOM", filters=filters, pluck="automotive_revision")


def before_validate(doc, method=None) -> None:
	for row in doc.items:
		if flt(row.automotive_net_qty):
			row.qty = flt(flt(row.automotive_net_qty) * (1 + flt(row.automotive_scrap_factor_percent) / 100), 6)


def validate(doc, method=None) -> None:
	if not doc.automotive_revision:
		doc.automotive_revision = next_revision(item_revisions(doc.item, exclude=doc.name))
	if doc.automotive_effective_from and doc.automotive_effective_to and getdate(doc.automotive_effective_to) < getdate(
		doc.automotive_effective_from
	):
		frappe.throw(_("Effective To cannot be before Effective From."))


def on_submit(doc, method=None) -> None:
	values = {"automotive_engineering_status": "Released"}
	if not doc.automotive_effective_from:
		values["automotive_effective_from"] = nowdate()
	doc.db_set(values)
	if getdate(doc.automotive_effective_from or nowdate()) > getdate(nowdate()) and doc.is_default:
		# a future revision must not become the default before it is effective
		doc.db_set("is_default", 0)
		previous = doc.automotive_supersedes
		if previous:
			frappe.db.set_value("BOM", previous, "is_default", 1)
			frappe.db.set_value("Item", doc.item, "default_bom", previous)


def on_update_after_submit(doc, method=None) -> None:
	if frappe.flags.get("automotive_eco_context"):
		return
	before = doc.get_doc_before_save()
	if not before:
		return
	for field in ("automotive_effective_from", "automotive_effective_to", "automotive_superseded_by",
			"automotive_revision", "automotive_engineering_status"):
		if before.get(field) != doc.get(field):
			raise EngineeringControlError(_("{0} of a released BOM changes only through an Engineering Change Order.").format(
				doc.meta.get_label(field)))
	if before.is_active and not doc.is_active:
		open_orders = frappe.get_all("Work Order", filters={"bom_no": doc.name, "docstatus": 1,
			"status": ("not in", ("Completed", "Closed", "Stopped", "Cancelled"))}, pluck="name", limit=5)
		if open_orders:
			raise EngineeringControlError(_("BOM {0} still feeds open Work Orders: {1}").format(doc.name, ", ".join(open_orders)))


def before_cancel(doc, method=None) -> None:
	used = frappe.get_all("Work Order", filters={"bom_no": doc.name, "docstatus": 1}, pluck="name", limit=1)
	if used:
		raise EngineeringControlError(
			_("BOM {0} was used by Work Order {1}; it is part of production history and cannot be cancelled. "
			  "Supersede it with an Engineering Change Order.").format(doc.name, used[0])
		)


def guard_bom_update_tool(doc, method=None) -> None:
	if not frappe.flags.get("automotive_eco_context"):
		raise EngineeringControlError(
			_("Replacing BOMs inside released BOMs is done through an Engineering Change Order, not the BOM Update Tool.")
		)


def effective_bom(item_code: str, on_date=None) -> str | None:
	day = getdate(on_date or nowdate())
	rows = frappe.db.sql(
		"""select name from `tabBOM`
		where item=%(item)s and docstatus=1 and is_active=1
		and ifnull(automotive_effective_from, '1900-01-01') <= %(day)s
		and ifnull(automotive_effective_to, '9999-12-31') >= %(day)s
		order by ifnull(automotive_effective_from, '1900-01-01') desc, is_default desc, creation desc
		limit 1""",
		{"item": item_code, "day": day},
	)
	return rows[0][0] if rows else None


def select_effective_bom(doc, method=None) -> None:
	"""Work Order before_validate: use the BOM effective at the planned start."""
	if doc.docstatus != 0 or doc.automotive_bom_pinned or not doc.production_item:
		return
	bom = effective_bom(doc.production_item, doc.planned_start_date or nowdate())
	if bom and bom != doc.bom_no:
		doc.bom_no = bom
		doc.required_items = []  # regenerate from the selected revision
		doc.operations = []
		doc.flags.automotive_bom_reselected = True


def validate_work_order_bom(doc, method=None) -> None:
	if doc.docstatus != 0 or not doc.bom_no:
		return
	if doc.flags.get("automotive_bom_reselected") and frappe.get_meta("Work Order").has_field("operations"):
		doc.set_work_order_operations()
	status, start, end = frappe.db.get_value(
		"BOM", doc.bom_no, ["automotive_engineering_status", "automotive_effective_from", "automotive_effective_to"]
	)
	day = getdate(doc.planned_start_date or nowdate())
	if status not in ("Released", "Superseded", None, ""):
		frappe.throw(_("BOM {0} is {1}, not released for production.").format(doc.bom_no, status))
	if (start and getdate(start) > day) or (end and getdate(end) < day):
		frappe.throw(
			_("BOM {0} (revision {1}) is not effective on {2}. Effective {3} to {4}.").format(
				doc.bom_no, frappe.db.get_value("BOM", doc.bom_no, "automotive_revision"), day, start or "-", end or "-"),
			EngineeringControlError,
		)


def apply_effectivity() -> None:
	"""Daily: make the revision effective today the default and mark superseded ones."""
	today = getdate(nowdate())
	for bom in frappe.get_all(
		"BOM",
		filters={"docstatus": 1, "is_active": 1, "automotive_effective_from": ("<=", today),
			"automotive_engineering_status": "Released", "automotive_supersedes": ("is", "set")},
		fields=["name", "item", "automotive_supersedes", "is_default"],
	):
		if not bom.is_default:
			frappe.flags.automotive_eco_context = True
			try:
				doc = frappe.get_doc("BOM", bom.name)
				doc.is_default = 1
				doc.save(ignore_permissions=True)
			finally:
				frappe.flags.automotive_eco_context = False
		frappe.db.set_value("BOM", bom.automotive_supersedes, "automotive_engineering_status", "Superseded")
	commit()


def superseded_effective_to(effective_date) -> str:
	return str(add_days(getdate(effective_date), -1))
