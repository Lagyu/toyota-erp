"""Kanban pull replenishment with JIT delivery windows and stockout escalation (ERP-04, J-02).

A loop has N cards of one container quantity. A card is *Full* while its container
is at line side. Signals are raised when

* an emptied card is scanned (``scan_card``), or
* consumption from the line-side warehouse (any submitted Stock Entry, typically
  Manufacture backflush) leaves fewer containers than Full cards
  (ceil(actual / container) < full cards)  -- the consumption trigger.

Each signal gets a due time (first delivery window after lead time) and a fulfilment
document: a submitted *Material Request (Purchase)* for supplier loops (the supply-
chain app releases it against the blanket order / supplier schedule), or a draft
*Material Transfer* for internal loops. Receipt into the line-side warehouse closes
the signal and returns the card to *Full*. Overdue signals escalate.
"""

from __future__ import annotations

import datetime as dt
import math

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, get_datetime, get_time, now_datetime, time_diff_in_hours

from automotive_core.utils import commit

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
OPEN_STATUSES = ("Open", "Ordered", "Shipped", "Escalated")


def due_by(loop, signaled_at: dt.datetime) -> dt.datetime:
	earliest = add_to_date(get_datetime(signaled_at), hours=flt(loop.lead_time_hours))
	windows = sorted(loop.delivery_windows, key=lambda w: (WEEKDAYS.index(w.weekday), str(w.window_start)))
	if not windows:
		return earliest
	for offset in range(0, 15):
		day = (earliest + dt.timedelta(days=offset)).date()
		for window in windows:
			if WEEKDAYS[day.weekday()] != window.weekday:
				continue
			start = dt.datetime.combine(day, get_time(window.window_start))
			end = dt.datetime.combine(day, get_time(window.window_end))
			if end >= earliest:
				return max(start, earliest)
	return earliest


def recommended_cards(loop) -> int:
	days = flt(loop.lead_time_hours) / 24.0
	if not flt(loop.container_qty):
		return 0
	return int(math.ceil(flt(loop.daily_demand) * days / flt(loop.container_qty)) + (loop.safety_cards or 0))


def ensure_cards(loop) -> None:
	existing = frappe.get_all("Kanban Card", filters={"kanban_loop": loop.name}, pluck="card_no")
	for card_no in range(1, (loop.number_of_cards or 0) + 1):
		if card_no in existing:
			continue
		frappe.get_doc({"doctype": "Kanban Card", "card_id": f"{loop.name}-{card_no:02d}", "kanban_loop": loop.name,
			"card_no": card_no, "status": "Full"}).insert(ignore_permissions=True)


def create_signal(loop_name: str, source: str, card: str | None = None) -> str:
	loop = frappe.get_doc("Kanban Loop", loop_name)
	if loop.status != "Active":
		frappe.throw(_("Kanban loop {0} is suspended.").format(loop.name))
	if card:
		card_doc = frappe.get_doc("Kanban Card", card, for_update=True)
		if card_doc.kanban_loop != loop.name:
			frappe.throw(_("Card {0} does not belong to loop {1}.").format(card, loop.name))
		if card_doc.status != "Full":
			return card_doc.current_signal  # re-scan of an already signalled card is idempotent
	else:
		full = frappe.get_all("Kanban Card", filters={"kanban_loop": loop.name, "status": "Full"}, pluck="name",
			order_by="card_no asc", limit=1)
		card_doc = frappe.get_doc("Kanban Card", full[0], for_update=True) if full else None
	now = now_datetime()
	signal = frappe.get_doc(
		{
			"doctype": "Kanban Signal",
			"kanban_loop": loop.name,
			"kanban_card": card_doc.name if card_doc else None,
			"item_code": loop.item_code,
			"qty": loop.container_qty,
			"plant": loop.plant,
			"signal_source": source,
			"signaled_at": now,
			"due_by": due_by(loop, now),
			"status": "Open",
		}
	)
	signal.flags.ignore_permissions = True
	signal.insert()
	if card_doc:
		card_doc.db_set({"status": "Signaled", "current_signal": signal.name, "last_scan_at": now})
	_request_supply(loop, signal)
	loop.db_set("last_signal_at", now, update_modified=False)
	if not card_doc:
		signal.db_set({"status": "Escalated", "escalation_level": 1,
			"escalation_note": _("No full card left in the loop: line-side stock-out risk.")})
	return signal.name


def _request_supply(loop, signal) -> None:
	company = frappe.get_cached_value("Plant", loop.plant, "company")
	if loop.supply_type == "Supplier":
		mr = frappe.get_doc(
			{
				"doctype": "Material Request",
				"material_request_type": "Purchase",
				"company": company,
				"transaction_date": signal.signaled_at.date() if hasattr(signal.signaled_at, "date") else None,
				"schedule_date": get_datetime(signal.due_by).date(),
				"automotive_kanban_signal": signal.name,
				"set_warehouse": loop.line_side_warehouse,
				"items": [{
					"item_code": loop.item_code,
					"qty": loop.container_qty,
					"schedule_date": get_datetime(signal.due_by).date(),
					"warehouse": loop.line_side_warehouse,
				}],
			}
		)
		mr.flags.ignore_permissions = True
		mr.insert()
		mr.submit()
		signal.db_set({"material_request": mr.name, "status": "Ordered"})
	else:
		se = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"purpose": "Material Transfer",
				"stock_entry_type": "Material Transfer",
				"company": company,
				"automotive_source_doctype": "Kanban Signal",
				"automotive_source_name": signal.name,
				"items": [{
					"item_code": loop.item_code,
					"qty": loop.container_qty,
					"s_warehouse": loop.source_warehouse,
					"t_warehouse": loop.line_side_warehouse,
				}],
			}
		)
		se.flags.ignore_permissions = True
		se.insert()
		signal.db_set({"stock_entry": se.name, "status": "Ordered"})


def _complete(signal_name: str, receipt_field: str | None = None, receipt: str | None = None) -> None:
	signal = frappe.get_doc("Kanban Signal", signal_name)
	if signal.status not in OPEN_STATUSES:
		return
	now = now_datetime()
	values = {"status": "Received", "received_at": now,
		"lead_time_hours_actual": round(time_diff_in_hours(now, signal.signaled_at), 2)}
	if receipt_field:
		values[receipt_field] = receipt
	signal.db_set(values)
	if signal.kanban_card:
		frappe.db.set_value("Kanban Card", signal.kanban_card, {"status": "Full", "current_signal": None})


def _reopen(signal_name: str) -> None:
	signal = frappe.get_doc("Kanban Signal", signal_name)
	signal.db_set({"status": "Ordered", "received_at": None, "lead_time_hours_actual": None, "purchase_receipt": None})
	if signal.kanban_card:
		frappe.db.set_value("Kanban Card", signal.kanban_card, {"status": "Signaled", "current_signal": signal.name})


# ---------------------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------------------


def _line_side(warehouse: str | None) -> bool:
	return bool(warehouse) and frappe.get_cached_value("Warehouse", warehouse, "automotive_warehouse_role") == "Line Side"


def check_consumption(item_code: str, warehouse: str) -> list[str]:
	loops = frappe.get_all("Kanban Loop", filters={"item_code": item_code, "line_side_warehouse": warehouse,
		"status": "Active"}, fields=["name", "container_qty"])
	created = []
	for loop in loops:
		actual = flt(frappe.db.get_value("Bin", {"item_code": item_code, "warehouse": warehouse}, "actual_qty"))
		full = frappe.db.count("Kanban Card", {"kanban_loop": loop.name, "status": "Full"})
		needed = math.ceil(max(actual, 0) / flt(loop.container_qty)) if flt(loop.container_qty) else full
		for _i in range(max(full - needed, 0)):
			created.append(create_signal(loop.name, "Consumption Trigger"))
	return created


def on_stock_entry_submit(doc, method=None) -> None:
	if doc.get("automotive_source_doctype") == "Kanban Signal" and doc.purpose == "Material Transfer":
		_complete(doc.automotive_source_name, "stock_entry", doc.name)
	consumed = sorted({(row.item_code, row.s_warehouse) for row in doc.items if _line_side(row.s_warehouse)})
	for item_code, warehouse in consumed:
		check_consumption(item_code, warehouse)


def on_stock_entry_cancel(doc, method=None) -> None:
	if doc.get("automotive_source_doctype") == "Kanban Signal":
		_reopen(doc.automotive_source_name)


def _signals_for_receipt(doc) -> list[tuple[str, float, str]]:
	out = []
	for row in doc.items:
		if not row.get("material_request"):
			continue
		signal = frappe.db.get_value("Material Request", row.material_request, "automotive_kanban_signal")
		if signal:
			out.append((signal, flt(row.qty), row.warehouse))
	return out


def on_purchase_receipt_submit(doc, method=None) -> None:
	if doc.get("is_return"):
		return
	for signal, _qty, _warehouse in _signals_for_receipt(doc):
		_complete(signal, "purchase_receipt", doc.name)


def on_purchase_receipt_cancel(doc, method=None) -> None:
	for signal, _qty, _warehouse in _signals_for_receipt(doc):
		if frappe.db.get_value("Kanban Signal", signal, "purchase_receipt") == doc.name:
			_reopen(signal)


# ---------------------------------------------------------------------------------------
# APIs and scheduler
# ---------------------------------------------------------------------------------------


@frappe.whitelist(methods=["POST"])
def scan_card(card_id: str) -> dict:
	"""Shop-floor scan of an emptied card (barcode = card id). Re-scans are idempotent."""
	frappe.has_permission("Kanban Card", "read", doc=card_id, throw=True)
	card = frappe.get_doc("Kanban Card", card_id)
	signal = create_signal(card.kanban_loop, "Card Scan", card=card.name)
	return {"signal": signal, "status": frappe.db.get_value("Kanban Signal", signal, "status")}


def escalate_overdue() -> None:
	from automotive_core.governance.notifications import notify

	now = now_datetime()
	overdue = frappe.get_all("Kanban Signal", filters={"status": ("in", ("Open", "Ordered", "Shipped")),
		"due_by": ("<", now)}, fields=["name", "kanban_loop", "item_code", "plant", "escalation_level", "due_by"])
	for row in overdue:
		level = (row.escalation_level or 0) + 1
		frappe.db.set_value("Kanban Signal", row.name, {"status": "Escalated", "escalation_level": level,
			"escalation_note": _("Overdue since {0}").format(row.due_by)})
		supervisors = frappe.get_all("Plant Assignment", filters={"plant": row.plant, "status": "Active",
			"assignment_role": ("in", ("Supervisor", "Logistics", "Warehouse"))}, pluck="user")
		notify("kanban.overdue", sorted({u for u in supervisors if u}), "Kanban Signal", row.name)
	commit()
