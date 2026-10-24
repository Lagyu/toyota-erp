"""Load test of the Hinode ERP: desk personas, partner portals and background jobs.

Requirements PRF-2, PRF-3, TST-9 and GATE-7: 50 concurrent users plus background jobs and
a 150-user peak on the standard dataset; p95 of list and detail views <= 2 s, of form
submissions <= 4 s. Run it through ``scripts/perf/run.sh`` (pinned Locust, results under
``perf/results/raw/<timestamp>/``); the procedure is ``ops/performance/README.md``.

Every request is named ``read:<operation> <object>`` or ``write:<operation> <object>``
and uses GET for reads, so the results group like the server's own latency histogram
(``erp_http_request_duration_seconds``: read = GET/HEAD, write = any other method).
``job:`` entries are completion times of background jobs, not HTTP requests.

User classes:

- ``DeskUser``: an internal user, one dataset persona per virtual user in the head-count
  mix of ``PERSONAS``, signed in through ``/api/method/login`` with the credentials that
  ``automotive_seed.e2e.prepare_users`` writes. About 80 % of its actions read (list views
  with their count, forms opened from a list, workspaces with their number cards,
  reports), 20 % write things that are safe to repeat on a reference site: a ToDo
  (created, closed the next time), a comment on a document the user opened, a draft
  Material Request and, for warehouse and logistics staff, a submitted 1-unit Material
  Transfer between a line side and the parts store of their plant (moved back by their
  next transfer).
- ``SupplierPortalUser`` and ``DealerPortalUser``: the partner portals' JSON API, limited
  to the functions their portal role allows (``capabilities`` of the dashboard).
- ``BackgroundPlanner`` (one user): a planner starting an MRP Planning Run (long queue)
  every ``--planning-interval`` seconds and waiting for it.

Never point it at production: the write scenarios create documents. The credentials only
exist on reference sites (``prepare_users`` refuses to run anywhere else).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import gevent
from locust import HttpUser, events, task
from locust.exception import StopUser

log = logging.getLogger("perf")

API = "/api/method/"
CSRF = re.compile(r'frappe\.csrf_token\s*=\s*"([^"]+)"')
USER_AGENT = "hinode-perf (locust)"
SAVE = "frappe.desk.form.save.savedocs"


@dataclass(frozen=True)
class Persona:
	"""What one kind of desk user does. Workspaces and reports are the ones its role opens
	(roles of the Workspace and Report definitions, checked on the small dataset)."""

	weight: float  # share of the desk users (head count)
	plant: str | None  # plant of the dataset user (automotive_seed.e2e.PERSONAS)
	workspaces: tuple[str, ...]
	lists: tuple[str, ...]
	reports: tuple[str, ...] = ()
	writes: tuple[str, ...] = ()  # besides ToDo and comment: mr_transfer, mr_purchase, stock_transfer


AICHI = "AICHI-VA"
PERSONAS: dict[str, Persona] = {
	"warehouse": Persona(
		6,
		AICHI,
		("Logistics", "Supplier Collaboration"),
		(
			"Stock Entry",
			"Purchase Receipt",
			"Advance Shipping Notice",
			"Material Request",
			"Bin",
			"Cycle Count",
			"Receipt Discrepancy",
		),
		("Inventory Position",),
		("mr_transfer", "stock_transfer"),
	),
	"planner": Persona(
		3,
		AICHI,
		("Production Planning", "Shop Floor", "Logistics"),
		(
			"Work Order",
			"Planning Run",
			"Production Plan",
			"Material Request",
			"Plant Schedule",
			"Planning Parameter Set",
		),
		("Production Variance", "WIP by Line", "Inventory Position"),
		("mr_transfer",),
	),
	"buyer": Persona(
		3,
		None,
		("Supplier Collaboration", "Procurement Controls", "Production Planning"),
		(
			"Purchase Order",
			"Material Request",
			"Supplier",
			"Request for Quotation",
			"Supplier Quotation",
			"Blanket Order",
			"Supplier Delivery Schedule",
			"Advance Shipping Notice",
		),
		("Supplier Performance", "Purchase Price Variance"),
		("mr_purchase",),
	),
	"supervisor": Persona(
		3,
		AICHI,
		("Shop Floor", "Production Planning"),
		("Work Order", "Job Card", "Andon Event", "Shift Handover", "Downtime Entry", "Quality Hold"),
		("OEE by Line", "WIP by Line", "Production Variance"),
	),
	"logistics": Persona(
		3,
		AICHI,
		("Logistics", "Dealer Operations"),
		("Delivery Note", "Transport Booking", "Shipment Manifest", "Stock Entry"),
		("Goods In Transit", "Inventory Position"),
		("stock_transfer",),
	),
	"operator": Persona(3, AICHI, ("Shop Floor",), ("Job Card", "Work Order", "Downtime Entry")),
	"inspector": Persona(
		2,
		AICHI,
		("Quality Control", "Traceability"),
		("Quality Inspection", "Non Conformance", "Quality Hold", "Purchase Receipt"),
	),
	"na_warehouse": Persona(
		2,
		"NA-PDC",
		("Logistics",),
		("Stock Entry", "Purchase Receipt", "Delivery Note", "Vehicle Unit"),
		("Inventory Position",),
		("mr_purchase", "stock_transfer"),
	),
	"quality_manager": Persona(
		2,
		AICHI,
		("Quality Control", "Traceability", "Supplier Collaboration"),
		("Quality Hold", "Quality Disposition", "Non Conformance", "Quality Inspection"),
	),
	"ap_clerk": Persona(
		2,
		None,
		("Procurement Controls", "Finance Controls"),
		(
			"Purchase Invoice",
			"Invoice Match Exception",
			"Purchase Receipt",
			"Payment Entry",
			"Purchase Order",
		),
		("Purchase Price Variance",),
	),
	"dealer_liaison": Persona(
		2,
		None,
		("Dealer Operations", "Traceability"),
		("Dealer Order", "Dealer Order Change", "Issue", "Sales Order", "Vehicle Unit"),
	),
	"engineer": Persona(
		2,
		AICHI,
		("Product Engineering",),
		("Item", "BOM", "Engineering Change Request", "Engineering Change Order", "Vehicle Model"),
		("BOM Where Used",),
	),
	"planner_ja": Persona(
		1,
		AICHI,
		("Production Planning",),
		("Work Order", "Planning Run", "Material Request"),
		("Inventory Position",),
		("mr_transfer",),
	),
	"kyushu_logistics": Persona(
		1,
		"KYUSHU-PT",
		("Logistics",),
		("Delivery Note", "Stock Entry", "Transport Booking"),
		("Goods In Transit",),
		("stock_transfer",),
	),
	"quality_manager_2": Persona(
		1,
		AICHI,
		("Quality Control", "Traceability"),
		("Quality Hold", "Quality Disposition", "Non Conformance"),
	),
	"procurement_manager": Persona(
		1,
		None,
		("Procurement Controls", "Supplier Collaboration", "Governance"),
		(
			"Purchase Order",
			"Approval Request",
			"Supplier",
			"Blanket Order",
			"Sourcing Decision",
			"Supplier Onboarding",
		),
		("Supplier Performance",),
		("mr_purchase",),
	),
	"engineering_manager": Persona(
		1,
		AICHI,
		("Product Engineering",),
		("BOM", "Engineering Change Order", "Engineering Change Request", "Item"),
		("BOM Where Used",),
	),
	"treasury": Persona(
		1,
		None,
		("Finance Controls", "Procurement Controls"),
		("Payment Entry", "Purchase Invoice", "Approval Request", "Bank Account"),
	),
	"treasury_2": Persona(1, None, ("Finance Controls",), ("Payment Entry", "Approval Request")),
	"sales_manager": Persona(
		1,
		None,
		("Dealer Operations",),
		("Sales Order", "Dealer Order", "Vehicle Allocation", "Customer", "Dealer Registration"),
	),
	"plant_director": Persona(
		1,
		AICHI,
		("Management", "Production Planning", "Shop Floor", "Quality Control", "Logistics"),
		("Work Order", "Quality Hold", "Andon Event", "Stock Entry", "Vehicle Unit"),
		("OEE by Line", "WIP by Line", "Production Variance", "Inventory Position"),
	),
	"controller": Persona(
		1,
		None,
		("Finance Controls", "Management", "Intercompany", "Procurement Controls"),
		("Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry", "Period Close Checklist"),
		("Group Consolidation", "Intercompany Reconciliation", "Purchase Price Variance"),
	),
	"executive": Persona(
		1,
		None,
		("Management", "Production Planning", "Dealer Operations", "Supplier Collaboration"),
		("Sales Order", "Purchase Order", "Work Order", "Stock Entry", "Quality Inspection"),
		("Supplier Performance", "Production Variance", "OEE by Line", "Group Consolidation"),
	),
	"auditor": Persona(
		1,
		None,
		("Governance", "Management"),
		(
			"Audit Event",
			"Stock Ledger Entry",
			"GL Entry",
			"Stock Entry",
			"Purchase Order",
			"Approval Request",
		),
		("Segregation of Duties Review", "Group Consolidation", "Inventory Position"),
	),
	"it_operator": Persona(
		1,
		None,
		("Integration Hub", "Automotive Setup"),
		(
			"Integration Inbound Message",
			"Integration Outbound Message",
			"Error Log",
			"Integration Partner",
			"Audit Event",
		),
		("Segregation of Duties Review",),
	),
}
WRITE_WEIGHTS = {"todo": 1, "comment": 1, "mr_transfer": 2, "mr_purchase": 2, "stock_transfer": 3}
NOT_DISCUSSED = {"Audit Event", "Bin", "Error Log", "GL Entry", "Stock Ledger Entry"}

# What the desk list view asks for (Frappe v16, frappe.desk.reportview.get with the comment
# count): the standard columns (frappe.model.std_fields_list), then each DocType's title,
# status and in_list_view fields.
STANDARD_FIELDS = (
	"name",
	"owner",
	"creation",
	"modified",
	"modified_by",
	"_user_tags",
	"_assign",
	"_liked_by",
	"docstatus",
	"idx",
)
LIST_FIELDS = {
	"Advance Shipping Notice": "supplier expected_arrival status",
	"Andon Event": "reason_code production_line workstation stop_type status raised_at",
	"Approval Request": "reference_doctype reference_name status",
	"Audit Event": "event_type reference_doctype reference_name actor event_time",
	"BOM": "item is_active is_default total_cost has_variants",
	"Bank Account": "account_name account company",
	"Bin": "item_code warehouse actual_qty reserved_qty ordered_qty",
	"Blanket Order": "blanket_order_type",
	"Customer": "customer_name customer_type customer_group territory default_currency",
	"Cycle Count": "warehouse count_date status",
	"Dealer Order": "dealer status promised_date",
	"Dealer Order Change": "change_type dealer_order status",
	"Dealer Registration": "dealer_name status",
	"Delivery Note": "customer_name posting_date grand_total per_billed status per_returned",
	"Downtime Entry": "workstation from_time to_time operator",
	"Engineering Change Order": "title effective_date implementation_status",
	"Engineering Change Request": "title priority status",
	"Error Log": "method reference_doctype reference_name",
	"GL Entry": "posting_date transaction_date account cost_center",
	"Integration Inbound Message": "message_id partner message_type status",
	"Integration Outbound Message": "partner event_type status",
	"Integration Partner": "partner_name partner_code adapter connection_status",
	"Invoice Match Exception": "exception_type purchase_invoice item_code status",
	"Issue": "subject raised_by status priority",
	"Item": "item_name item_group stock_uom is_fixed_asset is_sales_item",
	"Job Card": "operation work_order workstation for_quantity status",
	"Journal Entry": "title company total_debit",
	"Material Request": "title material_request_type schedule_date transaction_date status",
	"Non Conformance": "subject procedure status",
	"Payment Entry": "title payment_type posting_date paid_from paid_to status",
	"Period Close Checklist": "company period_start period_end status",
	"Plant Schedule": "production_line schedule_date status planned_units",
	"Planning Parameter Set": "parameter_set version",
	"Planning Run": "plant status",
	"Production Plan": "company get_items_from posting_date item_code customer status",
	"Purchase Invoice": "supplier_name due_date posting_date bill_no grand_total status",
	"Purchase Order": "supplier_name transaction_date schedule_date grand_total per_received per_billed status",
	"Purchase Receipt": "supplier_name posting_date grand_total per_billed per_returned status",
	"Quality Disposition": "decision item_code qty",
	"Quality Hold": "reason hold_scope item status",
	"Quality Inspection": "report_date inspection_type reference_name item_code status",
	"Receipt Discrepancy": "item_code supplier discrepancy_type status",
	"Request for Quotation": "company vendor transaction_date status schedule_date",
	"Sales Invoice": "customer_name posting_date due_date grand_total status",
	"Sales Order": "customer_name delivery_date grand_total status per_delivered per_billed",
	"Shift Handover": "production_line shift_date shift_code status",
	"Shipment Manifest": "carrier departure delivery_status",
	"Sourcing Decision": "item_code awarded_supplier",
	"Stock Entry": "stock_entry_type purpose from_warehouse to_warehouse per_transferred is_return",
	"Stock Ledger Entry": "item_code warehouse posting_date voucher_no actual_qty incoming_rate",
	"Supplier": "supplier_name supplier_group supplier_type default_currency",
	"Supplier Delivery Schedule": "supplier schedule_version status",
	"Supplier Onboarding": "supplier_name status",
	"Supplier Quotation": "supplier_name transaction_date grand_total valid_till status",
	"Transport Booking": "carrier direction pickup_window_start status",
	"Vehicle Allocation": "vehicle_unit sales_order status",
	"Vehicle Model": "model_name model_code lifecycle_status",
	"Vehicle Unit": "vin vehicle_model variant_item production_status quality_status",
	"Work Order": "production_item bom_no qty status",
}
# filters the users of these lists typically keep (open work, posted documents)
LIST_FILTERS = {
	"Approval Request": [["status", "=", "Pending"]],
	"Bin": [["actual_qty", ">", 0]],
	"Purchase Invoice": [["docstatus", "=", 1]],
	"Purchase Order": [["status", "not in", ["Closed", "Completed", "Cancelled"]]],
	"Sales Invoice": [["docstatus", "=", 1]],
	"Stock Entry": [["docstatus", "=", 1]],
	"Work Order": [["status", "not in", ["Completed", "Closed", "Cancelled", "Stopped"]]],
}
# report -> (days before --as-of for from_date, name of the end-date filter, filter taken
# from a document the user has seen: (fieldname, DocType)); mirrors the reports' defaults
REPORTS: dict[str, tuple[int | None, str | None, tuple[str, str] | None]] = {
	"BOM Where Used": (None, None, ("item_code", "Item")),
	"Goods In Transit": (None, "as_on_date", None),
	"Group Consolidation": (None, "to_date", None),
	"Intercompany Reconciliation": (None, "to_date", None),
	"Inventory Position": (None, None, None),
	"OEE by Line": (6, "to_date", None),
	"Production Variance": (29, "to_date", None),
	"Purchase Price Variance": (29, "to_date", None),
	"Segregation of Duties Review": (None, None, None),
	"Supplier Performance": (89, "to_date", None),
	"WIP by Line": (None, None, None),
}
JOB_POLL = 10.0  # seconds between status checks of a background job
JOB_TIMEOUT = 1800.0


@dataclass
class Settings:
	password: str = ""
	accounts: dict[str, list[str]] = field(default_factory=dict)
	as_of: dt.date = field(default_factory=dt.date.today)
	think: tuple[float, float] = (5.0, 15.0)
	planning_interval: float = 300.0
	timeout: float = 60.0


SETTINGS = Settings()


class JobFailed(Exception):
	pass


@events.init_command_line_parser.add_listener
def _arguments(parser) -> None:
	parser.add_argument(
		"--credentials",
		env_var="PERF_CREDENTIALS",
		default="",
		help="persona credentials JSON written by automotive_seed.e2e.prepare_users (mode 0600)",
	)
	parser.add_argument(
		"--as-of",
		env_var="PERF_AS_OF",
		default="",
		help="business date for report windows, YYYY-MM-DD (default today; use the dataset's last simulated day)",
	)
	parser.add_argument(
		"--think-time",
		env_var="PERF_THINK_TIME",
		default="5-15",
		help="seconds a user waits between actions, MIN-MAX (default 5-15)",
	)
	parser.add_argument(
		"--planning-interval",
		env_var="PERF_PLANNING_INTERVAL",
		type=float,
		default=300.0,
		help="seconds between background MRP runs of BackgroundPlanner, 0 to leave it out (default 300)",
	)
	parser.add_argument(
		"--request-timeout",
		env_var="PERF_REQUEST_TIMEOUT",
		type=float,
		default=60.0,
		help="seconds before a request counts as failed (default 60)",
	)
	parser.add_argument(
		"--personas",
		env_var="PERF_PERSONAS",
		default="",
		help="comma-separated persona keys to restrict the desk mix to (default: all with an account)",
	)


def _accounts(value) -> list[str]:
	users = value if isinstance(value, list) else [value]
	# prepare_users may name Administrator as a second approver; it is not a persona
	return [u for u in users if u and u != "Administrator"]


@events.init.add_listener
def _init(environment, **_kwargs) -> None:
	options = environment.parsed_options
	if options is None:
		return
	if not options.credentials:
		raise SystemExit("perf: --credentials FILE is required (automotive_seed.e2e.prepare_users output)")
	data = json.loads(Path(options.credentials).read_text())
	SETTINGS.password = data["password"]
	SETTINGS.accounts = {key: _accounts(value) for key, value in (data.get("users") or {}).items()}
	SETTINGS.as_of = dt.date.fromisoformat(options.as_of) if options.as_of else dt.date.today()
	low, _, high = str(options.think_time).partition("-")
	SETTINGS.think = (float(low), float(high or low))
	SETTINGS.planning_interval = float(options.planning_interval)
	SETTINGS.timeout = float(options.request_timeout)
	wanted = {p.strip() for p in options.personas.split(",") if p.strip()}
	desk = {
		key: p.weight
		for key, p in PERSONAS.items()
		if SETTINGS.accounts.get(key) and (not wanted or key in wanted)
	}
	DeskUser.picker = _SmoothRoundRobin(desk)
	available = {
		"DeskUser": bool(desk),
		"SupplierPortalUser": bool(SETTINGS.accounts.get("supplier")),
		"DealerPortalUser": bool(SETTINGS.accounts.get("dealer")),
		"BackgroundPlanner": SETTINGS.planning_interval > 0 and bool(BackgroundPlanner.account_key()),
	}
	environment.user_classes[:] = [
		cls for cls in environment.user_classes if available.get(cls.__name__, True)
	]
	log.info(
		"desk personas: %s; user classes: %s; report date %s",
		", ".join(desk) or "none",
		", ".join(cls.__name__ for cls in environment.user_classes),
		SETTINGS.as_of,
	)


class _SmoothRoundRobin:
	"""Weighted round robin whose every prefix follows the weights closely, so a small
	run gets the most common personas first and a large one the full mix."""

	def __init__(self, weights: dict[str, float]) -> None:
		self.weights = weights
		self.current = dict.fromkeys(weights, 0.0)

	def next(self) -> str:
		total = sum(self.weights.values())
		for key, weight in self.weights.items():
			self.current[key] += weight
		best = max(self.current, key=self.current.__getitem__)
		self.current[best] -= total
		return best


_ACCOUNT_TURN: dict[str, int] = {}


def account(key: str) -> str:
	"""The persona's accounts in turn: prepare_users gives one per persona, which all its
	virtual users share; a list in the credentials file is shared out."""
	accounts = SETTINGS.accounts[key]
	turn = _ACCOUNT_TURN.get(key, 0)
	_ACCOUNT_TURN[key] = turn + 1
	return accounts[turn % len(accounts)]


def _failure(response) -> str:
	"""HTTP status and Frappe exception type, so one problem aggregates in one failure row."""
	if not response.status_code:
		return f"no response ({type(response.error).__name__})"
	kind = ""
	try:
		body = response.json()
		kind = body.get("exc_type") or (body.get("exception") or "").split(":", 1)[0].rsplit(".", 1)[-1]
	except (ValueError, AttributeError):
		pass
	return f"HTTP {response.status_code} {kind}".strip()


def _server_message(response) -> str:
	try:
		messages = json.loads(response.json().get("_server_messages") or "[]")
		return "; ".join(json.loads(m).get("message", "") for m in messages)[:300]
	except (ValueError, AttributeError, TypeError):
		return ""


class ErpUser(HttpUser):
	abstract = True
	_reported: set = set()  # failures already logged once (class-wide)

	def wait_time(self) -> float:
		return random.uniform(*SETTINGS.think)

	def on_start(self) -> None:
		self.client.headers["User-Agent"] = USER_AGENT
		self.user_id = ""

	def call(
		self, verb: str, method: str, name: str, *, params=None, data=None, json_body=None, headers=None
	) -> dict | None:
		"""One API request; the decoded JSON body, or None after recording a failure."""
		with self.client.request(
			verb,
			API + method,
			name=name,
			params=params,
			data=data,
			json=json_body,
			headers=headers,
			timeout=SETTINGS.timeout,
			catch_response=True,
		) as response:
			if not response.ok:
				reason = _failure(response)
				response.failure(reason)
				if (name, reason) not in self._reported:
					self._reported.add((name, reason))
					log.warning("%s: %s %s", name, reason, _server_message(response))
				return None
			try:
				return response.json()
			except ValueError:
				response.failure("response is not JSON")
				return None

	def page(self, path: str, name: str) -> bool:
		"""An HTML page (desk or portal) and the session's CSRF token in it."""
		with self.client.get(path, name=name, timeout=SETTINGS.timeout, catch_response=True) as response:
			if not response.ok:
				response.failure(_failure(response))
				return False
			match = CSRF.search(response.text)
			if not match:
				response.failure("page without a CSRF token")
				return False
			self.client.headers["X-Frappe-CSRF-Token"] = match.group(1)
			return True

	def sign_in(self, user: str) -> None:
		if self.call("POST", "login", "write:login", data={"usr": user, "pwd": SETTINGS.password}) is None:
			raise StopUser
		self.user_id = user

	def get_list(
		self,
		doctype: str,
		fields,
		filters=(),
		*,
		limit: int = 20,
		start: int = 0,
		order_by: str = "modified desc",
		name: str | None = None,
	) -> list[dict]:
		body = self.call(
			"GET",
			"frappe.client.get_list",
			name or f"read:list {doctype}",
			params={
				"doctype": doctype,
				"fields": json.dumps(list(fields)),
				"filters": json.dumps(list(filters)),
				"order_by": order_by,
				"limit_start": start,
				"limit_page_length": limit,
			},
		)
		return (body or {}).get("message") or []

	def desk_list(self, doctype: str, fields, filters=(), *, limit: int = 20, start: int = 0) -> list[dict]:
		"""A page of the desk list view, as its own request (reportview.get, with the comment
		count; the answer comes as keys and value rows)."""
		body = self.call(
			"GET",
			"frappe.desk.reportview.get",
			f"read:list {doctype}",
			params={
				"doctype": doctype,
				"fields": json.dumps([f"`tab{doctype}`.`{field}`" for field in fields]),
				"filters": json.dumps(list(filters)),
				"order_by": f"`tab{doctype}`.`modified` desc",
				"start": start,
				"page_length": limit,
				"view": "List",
				"group_by": "",
				"with_comment_count": 1,
			},
		)
		data = (body or {}).get("message") or {}
		keys = data.get("keys") or []
		return [dict(zip(keys, row, strict=False)) for row in data.get("values") or []]


# ---------------------------------------------------------------------------------------
# Desk
# ---------------------------------------------------------------------------------------


@dataclass
class Stock:
	"""A plant's parts store and line sides, and the line-side bins a clerk may move from:
	items without batch or serial numbers, without an active Kanban loop at that line side
	(taking stock from it would raise a replenishment signal) and not on hold."""

	company: str
	store: str
	movable: list[dict]
	loaded: float


class DeskUser(ErpUser):
	weight = 6
	picker = _SmoothRoundRobin({})

	def on_start(self) -> None:
		super().on_start()
		self.persona_key = self.picker.next()
		self.persona = PERSONAS[self.persona_key]
		self.seen: dict[str, deque] = {}  # names from the list views, per DocType
		self.cards: dict[str, dict | None] = {}
		self.todo: str | None = None
		self.stock: Stock | None = None
		self.purchase: tuple[list[dict], list[dict]] | None = None
		self.returns: list[tuple[str, str, str, str]] = []
		self.sign_in(account(self.persona_key))
		self.page("/desk", "read:desk")

	# reads ------------------------------------------------------------------------------

	@task(46)
	def list_view(self) -> None:
		doctype = random.choice(self.persona.lists)
		filters = LIST_FILTERS.get(doctype, [])
		fields = (*STANDARD_FIELDS, *LIST_FIELDS.get(doctype, "").split())
		rows = self.desk_list(doctype, fields, filters, start=random.choice((0, 0, 0, 20)))
		self.call(
			"GET",
			"frappe.client.get_count",
			f"read:count {doctype}",
			params={"doctype": doctype, "filters": json.dumps(filters)},
		)
		if rows:
			self.seen.setdefault(doctype, deque(maxlen=20)).extend(r["name"] for r in rows)
			if random.random() < 0.6:
				self.open_form(doctype, random.choice(rows)["name"])

	def open_form(self, doctype: str, name: str) -> dict | None:
		body = self.call(
			"GET",
			"frappe.desk.form.load.getdoc",
			f"read:form {doctype}",
			params={"doctype": doctype, "name": name},
		)
		docs = (body or {}).get("docs") or []
		return docs[0] if docs else None

	@task(18)
	def workspace(self) -> None:
		name = random.choice(self.persona.workspaces)
		body = self.call(
			"GET",
			"frappe.desk.desktop.get_desktop_page",
			f"read:workspace {name}",
			params={"page": json.dumps({"name": name, "title": name, "public": 1})},
		)
		page = (body or {}).get("message") or {}
		for card in (page.get("number_cards") or {}).get("items") or []:
			self.number_card(card.get("number_card_name"))

	def number_card(self, card: str | None) -> None:
		if not card:
			return
		if card not in self.cards:  # the desk loads a card's definition once per session
			self.cards[card] = self.open_form("Number Card", card)
		doc = self.cards[card]
		if not doc:
			return
		name = f"read:card {doc.get('label') or card}"
		if doc.get("type") == "Custom" and doc.get("method"):
			self.call("GET", doc["method"], name, params={"filters": doc.get("filters_json") or "{}"})
		elif doc.get("type") == "Report" and doc.get("report_name"):
			self.call(
				"GET",
				"frappe.desk.query_report.run",
				name,
				params={
					"report_name": doc["report_name"],
					"filters": doc.get("filters_json") or "{}",
					"ignore_prepared_report": 1,
				},
			)
		else:
			card_doc = {
				k: doc.get(k)
				for k in (
					"document_type",
					"label",
					"function",
					"aggregate_function_based_on",
					"color",
					"filters_json",
				)
			}
			self.call(
				"GET",
				"frappe.desk.doctype.number_card.number_card.get_result",
				name,
				params={"doc": json.dumps(card_doc), "filters": doc.get("filters_json") or "[]"},
			)

	@task(14)
	def report(self) -> None:
		name = random.choice(self.persona.reports) if self.persona.reports else ""
		filters = self.report_filters(name) if name else None
		if filters is None:  # no report for this persona, or it needs a document not seen yet
			self.list_view()
			return
		self.call(
			"GET",
			"frappe.desk.query_report.run",
			f"read:report {name}",
			params={
				"report_name": name,
				"filters": json.dumps(filters),
				"ignore_prepared_report": 0,
				"are_default_filters": 1,
			},
		)

	def report_filters(self, name: str) -> dict | None:
		days, end_field, needs = REPORTS.get(name, (None, None, None))
		filters = {}
		if days is not None:
			filters["from_date"] = (SETTINGS.as_of - dt.timedelta(days=days)).isoformat()
		if end_field:
			filters[end_field] = SETTINGS.as_of.isoformat()
		if needs:
			seen = self.seen.get(needs[1])
			if not seen:
				return None
			filters[needs[0]] = random.choice(seen)
		return filters

	@task(2)
	def reload(self) -> None:
		self.page("/desk", "read:desk")

	# writes -----------------------------------------------------------------------------

	@task(20)
	def write(self) -> None:
		kinds = {"todo": 1, "comment": 1} | {kind: WRITE_WEIGHTS[kind] for kind in self.persona.writes}
		kind = random.choices(list(kinds), weights=list(kinds.values()))[0]
		# each returns False when the dataset offers nothing to do; a ToDo is always possible
		if not getattr(self, f"write_{kind}")():
			self.write_todo()

	def recent_document(self) -> tuple[str, str] | None:
		"""A business document the user listed (not ledger, log or audit rows: people do not
		comment on those, and sealed audit events refuse any update)."""
		doctypes = [d for d, names in self.seen.items() if names and d not in NOT_DISCUSSED]
		if not doctypes:
			return None
		doctype = random.choice(doctypes)
		return doctype, random.choice(self.seen[doctype])

	def write_todo(self) -> bool:
		if self.todo:  # close the one opened last time
			self.call(
				"POST",
				"frappe.client.set_value",
				"write:close ToDo",
				data={"doctype": "ToDo", "name": self.todo, "fieldname": "status", "value": "Closed"},
			)
			self.todo = None
			return True
		doc = {
			"doctype": "ToDo",
			"__islocal": 1,
			"name": "new-todo-1",
			"status": "Open",
			"priority": "Medium",
			"allocated_to": self.user_id,
			"description": "[perf] follow up",
		}
		reference = self.recent_document()
		if reference:
			doc.update(
				reference_type=reference[0],
				reference_name=reference[1],
				description=f"[perf] follow up {reference[0]} {reference[1]}",
			)
		body = self.call("POST", SAVE, "write:save ToDo", data={"doc": json.dumps(doc), "action": "Save"})
		docs = (body or {}).get("docs") or []
		self.todo = docs[0]["name"] if docs else None
		return True

	def write_comment(self) -> bool:
		reference = self.recent_document()
		if not reference:
			return False
		self.call(
			"POST",
			"frappe.desk.form.utils.add_comment",
			"write:comment",
			data={
				"reference_doctype": reference[0],
				"reference_name": reference[1],
				"content": "<p>[perf] checked</p>",
				"comment_email": self.user_id,
				"comment_by": self.user_id,
			},
		)
		return True

	def stock_view(self) -> Stock | None:
		"""The persona's plant as the clerk looks it up (refreshed every 10 minutes)."""
		if not self.persona.plant:
			return None
		if self.stock and time.monotonic() - self.stock.loaded < 600:
			return self.stock
		warehouses = self.get_list(
			"Warehouse",
			("name", "company", "automotive_warehouse_role"),
			[["automotive_plant", "=", self.persona.plant], ["is_group", "=", 0]],
			limit=200,
		)
		store = next((w for w in warehouses if w["automotive_warehouse_role"] == "Raw Material"), None)
		if not store:
			return None
		line_sides = [w["name"] for w in warehouses if w["automotive_warehouse_role"] == "Line Side"]
		movable: list[dict] = []
		if line_sides:
			bins = self.get_list(
				"Bin",
				("item_code", "warehouse", "actual_qty"),
				[["warehouse", "in", line_sides], ["actual_qty", ">=", 3]],
				limit=100,
				order_by="actual_qty desc",
			)
			items = {
				i["name"]: i
				for i in self.get_list(
					"Item",
					("name", "item_name", "stock_uom"),
					[
						["name", "in", sorted({b["item_code"] for b in bins}) or [""]],
						["has_batch_no", "=", 0],
						["has_serial_no", "=", 0],
						["is_stock_item", "=", 1],
					],
					limit=100,
				)
			}
			loops = {
				(r["item_code"], r["line_side_warehouse"])
				for r in self.get_list(
					"Kanban Loop",
					("item_code", "line_side_warehouse"),
					[["status", "=", "Active"], ["line_side_warehouse", "in", line_sides]],
					limit=1000,
				)
			}
			held = {
				r["item"]
				for r in self.get_list(
					"Quality Hold",
					("item",),
					[["status", "=", "Active"], ["hold_scope", "=", "Item"]],
					limit=500,
				)
			}
			movable = [
				{**b, **items[b["item_code"]]}
				for b in bins
				if b["item_code"] in items
				and (b["item_code"], b["warehouse"]) not in loops
				and b["item_code"] not in held
			]
		self.stock = Stock(store["company"], store["name"], movable, time.monotonic())
		return self.stock

	def write_stock_transfer(self) -> bool:
		stock = self.stock_view()
		if not stock:
			return False
		if self.returns:  # put back what the last transfer moved
			item, source, target, uom = self.returns.pop()
			back = True
		elif stock.movable:
			row = random.choice(stock.movable)
			item, source, target, uom = row["item_code"], row["warehouse"], stock.store, row["stock_uom"]
			back = False
		else:
			return False
		doc = {
			"doctype": "Stock Entry",
			"__islocal": 1,
			"name": "new-stock-entry-1",
			"stock_entry_type": "Material Transfer",
			"purpose": "Material Transfer",
			"company": stock.company,
			"remarks": "[perf] 1-unit transfer",
			"items": [
				{
					"doctype": "Stock Entry Detail",
					"__islocal": 1,
					"name": "new-stock-entry-detail-1",
					"item_code": item,
					"qty": 1,
					"transfer_qty": 1,
					"uom": uom,
					"stock_uom": uom,
					"conversion_factor": 1,
					"s_warehouse": source,
					"t_warehouse": target,
				}
			],
		}
		body = self.call(
			"POST", SAVE, "write:save Stock Entry", data={"doc": json.dumps(doc), "action": "Save"}
		)
		saved = ((body or {}).get("docs") or [None])[0]
		if (
			saved
			and self.call(
				"POST", SAVE, "write:submit Stock Entry", data={"doc": json.dumps(saved), "action": "Submit"}
			)
			and not back
		):
			self.returns.append((item, target, source, uom))
		return True

	def write_mr_transfer(self) -> bool:
		stock = self.stock_view()
		if not stock or not stock.movable:
			return False
		rows = random.sample(stock.movable, k=min(len(stock.movable), random.randint(1, 3)))
		target = rows[0]["warehouse"]
		self.save_material_request(
			"Material Transfer",
			stock.company,
			target,
			[(r["item_code"], r["item_name"], r["stock_uom"]) for r in rows if r["warehouse"] == target],
			from_warehouse=stock.store,
		)
		return True

	def write_mr_purchase(self) -> bool:
		if self.purchase is None:  # the stores the user may order into, and stock items to order
			stores = self.get_list(
				"Warehouse",
				("name", "company"),
				[["automotive_warehouse_role", "=", "Raw Material"], ["is_group", "=", 0]],
				limit=50,
			)
			items = self.get_list(
				"Item",
				("name", "item_name", "stock_uom"),
				[
					["is_purchase_item", "=", 1],
					["is_stock_item", "=", 1],
					["has_variants", "=", 0],
					["disabled", "=", 0],
				],
				limit=100,
			)
			self.purchase = (stores, items)
		stores, items = self.purchase
		if not stores or not items:
			return False
		store = random.choice(stores)
		rows = random.sample(items, k=min(len(items), random.randint(1, 4)))
		self.save_material_request(
			"Purchase",
			store["company"],
			store["name"],
			[(r["name"], r["item_name"], r["stock_uom"]) for r in rows],
		)
		return True

	def save_material_request(
		self,
		kind: str,
		company: str,
		warehouse: str,
		items: list[tuple[str, str, str]],
		from_warehouse: str | None = None,
	) -> None:
		today = dt.date.today()
		needed = (today + dt.timedelta(days=7)).isoformat()
		rows = [
			{
				"doctype": "Material Request Item",
				"__islocal": 1,
				"name": f"new-material-request-item-{i}",
				"item_code": code,
				"item_name": item_name,
				"description": item_name,
				"qty": random.randint(1, 20),
				"uom": uom,
				"stock_uom": uom,
				"conversion_factor": 1,
				"schedule_date": needed,
				"warehouse": warehouse,
				**({"from_warehouse": from_warehouse} if from_warehouse else {}),
			}
			for i, (code, item_name, uom) in enumerate(items, start=1)
		]
		doc = {
			"doctype": "Material Request",
			"__islocal": 1,
			"name": "new-material-request-1",
			"material_request_type": kind,
			"company": company,
			"transaction_date": today.isoformat(),
			"schedule_date": needed,
			"set_warehouse": warehouse,
			"items": rows,
		}
		self.call(
			"POST", SAVE, "write:save Material Request", data={"doc": json.dumps(doc), "action": "Save"}
		)


# ---------------------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------------------


class BackgroundPlanner(ErpUser):
	"""Starts an MRP Planning Run (queued on the long queue) and waits for it; the time
	from start to completion is recorded as ``job:Planning Run``."""

	fixed_count = 1

	@staticmethod
	def account_key() -> str | None:
		return next(
			(key for key in ("planner", "planner_ja", "plant_director") if SETTINGS.accounts.get(key)), None
		)

	def wait_time(self) -> float:
		return SETTINGS.planning_interval

	def on_start(self) -> None:
		super().on_start()
		key = self.account_key()
		self.sign_in(account(key))
		self.page("/desk", "read:desk")
		plant = PERSONAS[key].plant
		sets = self.get_list(
			"Planning Parameter Set", ("name", "company", "plant"), [["docstatus", "=", 1]], limit=50
		)
		self.parameter_sets = [s for s in sets if s["plant"] == plant] or sets
		if not self.parameter_sets:
			log.warning(
				"BackgroundPlanner: no submitted Planning Parameter Set visible; no background planning"
			)
			raise StopUser

	@task
	def plan(self) -> None:
		params = random.choice(self.parameter_sets)
		started = time.monotonic()
		body = self.call(
			"POST",
			"automotive_manufacturing.production_planning.planning_run.start",
			"write:start Planning Run",
			data={"company": params["company"], "plant": params["plant"], "parameter_set": params["name"]},
		)
		run = (body or {}).get("message")
		if not run:
			return
		status = None
		while time.monotonic() - started < JOB_TIMEOUT:
			gevent.sleep(JOB_POLL)
			body = self.call(
				"GET",
				"frappe.client.get_value",
				"read:poll Planning Run",
				params={
					"doctype": "Planning Run",
					"filters": run,
					"fieldname": json.dumps(["status", "progress"]),
				},
			)
			status = ((body or {}).get("message") or {}).get("status")
			if status in ("Completed", "Failed", "Cancelled"):
				break
		self.environment.events.request.fire(
			request_type="JOB",
			name="job:Planning Run",
			response_time=(time.monotonic() - started) * 1000,
			response_length=0,
			context={},
			exception=None
			if status == "Completed"
			else JobFailed(f"Planning Run {status or 'not finished'}"),
		)


# ---------------------------------------------------------------------------------------
# Partner portals
# ---------------------------------------------------------------------------------------


class PortalUser(ErpUser):
	abstract = True
	persona = ""
	page_path = ""
	module = ""
	# capability -> (list method, method showing one of its rows or None)
	reads: dict[str, tuple[tuple[str, str | None], ...]] = {}

	def on_start(self) -> None:
		super().on_start()
		self.sign_in(account(self.persona))
		self.page(self.page_path, f"read:portal {self.persona} page")
		self.capabilities = set((self.dashboard() or {}).get("capabilities") or ())

	def api(self, function: str, *, params=None) -> list | dict | None:
		body = self.call(
			"GET", f"{self.module}.{function}", f"read:portal {self.persona} {function}", params=params
		)
		return (body or {}).get("message")

	def dashboard(self) -> dict | None:
		message = self.api("dashboard")
		return message if isinstance(message, dict) else None

	def post(self, function: str, payload: dict) -> dict | None:
		"""A state-changing portal call, as the portal script sends it (JSON, CSRF token,
		a new Idempotency-Key)."""
		return self.call(
			"POST",
			f"{self.module}.{function}",
			f"write:portal {self.persona} {function}",
			json_body=payload,
			headers={"Idempotency-Key": str(uuid.uuid4())},
		)

	@task(80)
	def browse(self) -> None:
		options = [
			entry
			for capability, entries in self.reads.items()
			if capability in self.capabilities
			for entry in entries
		]
		if not options:
			self.overview()
			return
		function, detail = random.choice(options)
		rows = self.api(function)
		if detail and isinstance(rows, list) and rows and random.random() < 0.6:
			self.api(detail, params={"name": random.choice(rows)["name"]})
		self.after_list(function, rows)

	def after_list(self, function: str, rows) -> None:
		pass

	@task(14)
	def overview(self) -> None:
		self.dashboard()

	@task(3)
	def reload(self) -> None:
		self.page(self.page_path, f"read:portal {self.persona} page")

	@task(3)
	def write(self) -> None:
		if not self.portal_write():
			self.browse()

	def portal_write(self) -> bool:
		return False


class SupplierPortalUser(PortalUser):
	weight = 1
	persona = "supplier"
	page_path = "/supplier-portal"
	module = "automotive_portals.api.supplier"
	reads = {
		"orders": (("orders", "order"),),
		"shipments": (("asns", "asn"), ("open_lines", None)),
		"schedules": (("schedules", "schedule"),),
		"receipts": (("receipts", None), ("discrepancies", None)),
		"payments": (("remittances", None), ("invoices_status", None)),
		"quote": (("rfqs", "rfq"), ("quotations", None)),
		"notices": (("notices", None),),
	}

	def on_start(self) -> None:
		self.unread: list[str] = []
		super().on_start()

	def after_list(self, function: str, rows) -> None:
		if function == "notices" and isinstance(rows, list):
			self.unread = [r["name"] for r in rows if not r.get("read_at")]

	def portal_write(self) -> bool:
		if not self.unread:  # nothing to acknowledge until a notice arrives
			return False
		self.post("mark_notice_read", {"name": self.unread.pop()})
		return True


class DealerPortalUser(PortalUser):
	weight = 1
	persona = "dealer"
	page_path = "/dealer-portal"
	module = "automotive_portals.api.dealer"
	reads = {
		"catalogue": (("models", None),),
		"orders": (("orders", "order"),),
		"inventory": (("inventory", None),),
		"deliveries": (("deliveries", None),),
		"invoices": (("invoices", None), ("outstanding_balance", None)),
		"support": (("support_requests", None),),
	}

	def after_list(self, function: str, rows) -> None:
		# configuring a vehicle: price and promise date for one choice per option group
		if function == "models" and isinstance(rows, list) and rows and random.random() < 0.7:
			model = random.choice(rows)
			selections = {
				g["attribute"]: random.choice(g["values"])
				for g in model.get("options") or []
				if g.get("values")
			}
			self.api(
				"check_configuration",
				params={"vehicle_model": model["name"], "selections": json.dumps(selections), "qty": 1},
			)

	def portal_write(self) -> bool:
		if "raise_support" not in self.capabilities:
			return False
		self.post(
			"create_support_request",
			{
				"subject": "[perf] question about a delivery",
				"description": "Load test request, please ignore.",
			},
		)
		return True
