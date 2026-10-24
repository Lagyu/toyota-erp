"""End-to-end test fixtures on reference sites (never production).

``prepare_users`` picks one user of the synthetic dataset per persona and gives them a
password for this test run only (returned once, never stored), so the Playwright suite
logs in as real dataset users with their real roles and row-level scopes.

``prepare_journeys`` arranges the preconditions the browser journeys cannot create from
the screens without turning into other journeys (material at the line for a build to
order, an EDI partner's signing credentials), through the dataset's own flows.

    bench --site SITE execute automotive_seed.e2e.prepare_users --kwargs "{'output': PATH}"
    bench --site SITE execute automotive_seed.e2e.prepare_journeys --kwargs "{'output': PATH}"
"""

from __future__ import annotations

import datetime as dt
import json
import os

import frappe
from frappe.utils import flt, now_datetime, nowdate
from frappe.utils.password import update_password

from automotive_seed.runner import guard

# persona key -> (role, preferred plant or None for group-level staff)
PERSONAS = {
	"executive": ("Automotive Executive", None),
	"controller": ("Group Controller", None),
	"buyer": ("Buyer", None),
	"procurement_manager": ("Procurement Manager", None),
	"ap_clerk": ("AP Clerk", None),
	"treasury": ("Treasury Approver", None),
	"sales_manager": ("Sales Manager", None),
	"dealer_liaison": ("Dealer Liaison", None),
	"it_operator": ("IT Operator", None),
	"auditor": ("Auditor", None),
	"plant_director": ("Plant Director", "AICHI-VA"),
	"planner": ("Production Planner", "AICHI-VA"),
	"supervisor": ("Production Supervisor", "AICHI-VA"),
	"operator": ("Line Operator", "AICHI-VA"),
	"warehouse": ("Warehouse Clerk", "AICHI-VA"),
	"inspector": ("Quality Inspector", "AICHI-VA"),
	"quality_manager": ("Quality Manager", "AICHI-VA"),
	"engineer": ("Product Engineer", "AICHI-VA"),
	"engineering_manager": ("Engineering Manager", "AICHI-VA"),
	"logistics": ("Logistics Coordinator", "AICHI-VA"),
	"na_warehouse": ("Warehouse Clerk", "NA-PDC"),
}
PLANT = "AICHI-VA"


def _write(output: str, payload: str) -> None:
	fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
	with os.fdopen(fd, "w") as handle:
		handle.write(payload)


def _user_with(role: str, plant: str | None) -> str | None:
	rows = frappe.db.sql("""select u.name from `tabUser` u join `tabHas Role` r on r.parent = u.name
		where r.role = %s and u.enabled = 1 and u.user_type = 'System User' and u.name not in ('Administrator', 'Guest')
		order by u.name""", role, pluck=True)
	for user in rows:
		user_plant = frappe.db.get_value("User Permission", {"user": user, "allow": "Plant", "is_default": 1},
			"for_value")
		if user_plant == plant:
			return user
	return rows[0] if rows else None


def _partner(party_type: str) -> str | None:
	if party_type == "Supplier":  # a just-in-time supplier: the Kanban journey needs one
		rows = frappe.db.sql("""select pua.user from `tabPartner User Access` pua
			join `tabKanban Loop` kl on kl.supplier = pua.supplier and kl.status = 'Active'
			where pua.status = 'Active' and pua.party_type = 'Supplier' order by pua.user limit 1""", pluck=True)
		if rows:
			return rows[0]
	return frappe.db.get_value("Partner User Access", {"party_type": party_type, "status": "Active"}, "user",
		order_by="user asc")


def prepare_users(password: str | None = None, output: str | None = None) -> str:
	"""Returns the credentials JSON, or writes it to ``output`` (mode 0600) and returns a
	summary without the password."""
	guard()
	frappe.only_for("System Manager")
	password = password or frappe.generate_hash(length=24)
	users: dict[str, str] = {}
	for key, (role, plant) in PERSONAS.items():
		user = _user_with(role, plant)
		if user:
			users[key] = user
	users["supplier"] = _partner("Supplier")
	users["dealer"] = _partner("Customer")
	# a second treasury approver: payments need two distinct approvers
	others = frappe.db.sql("""select u.name from `tabUser` u join `tabHas Role` r on r.parent = u.name
		where r.role = 'Treasury Approver' and u.enabled = 1 and u.name != %s order by u.name""",
		users.get("treasury") or "", pluck=True)
	if others:
		users["treasury_2"] = others[0]
	for user in users.values():
		if user:
			update_password(user, password)
	frappe.db.commit()
	payload = json.dumps({"password": password, "users": users}, sort_keys=True)
	if output:
		_write(output, payload)
		return f"{len(users)} personas written to {output}"
	return payload


# ---------------------------------------------------------------------------------------
# Journey arrangement
# ---------------------------------------------------------------------------------------


def prepare_journeys(output: str | None = None) -> str | dict:
	"""Preconditions for the browser journeys, arranged at the current time through the
	dataset's own flows (never direct ledger writes):

	- ``j01`` build to order: a variant the e2e dealer may order with no vehicle waiting in
	  stock and no other order waiting for one, and one unit of its components staged at
	  the line side the way the generator stages a day's builds (store transfers, expedited
	  supplier deliveries with incoming inspection);
	- ``j09`` allocation from stock: a variant the dealer may order with a released vehicle
	  free in stock and no order waiting for it;
	- ``j10`` partner messaging: an EDI supplier with an open, unannounced order line and a
	  fresh inbound signing secret for its HTTPS channel (the test signs like the partner's
	  system would).

	Writes the JSON to ``output`` (mode 0600: it holds the partner secret) and returns a
	summary; without ``output`` returns the fixtures."""
	guard()
	frappe.only_for("System Manager")
	dealer = frappe.db.get_value("Partner User Access", {"user": _partner("Customer"), "status": "Active",
		"party_type": "Customer"}, "customer")
	registration = frappe.db.get_value("Customer", dealer, "automotive_dealer_registration")
	company = frappe.db.get_value("Dealer Registration", registration, "company")
	variants = _orderable(dealer, registration, company)
	fixtures = {"dealer": dealer, "company": company}
	fixtures["j09"] = _from_stock(variants)
	fixtures["j01"] = _build_to_order(variants, exclude={(fixtures["j09"] or {}).get("variant")})
	fixtures["j10"] = _edi_supplier()
	frappe.db.commit()
	if output:
		_write(output, json.dumps(fixtures, sort_keys=True, default=str))
		staged = len((fixtures["j01"] or {}).get("staged") or [])
		return (f"journeys: j01 {(fixtures['j01'] or {}).get('variant')} ({staged} components staged), "
			f"j09 {(fixtures['j09'] or {}).get('variant')}, j10 {(fixtures['j10'] or {}).get('partner')}")
	return fixtures


def _orderable(dealer: str, registration: str, company: str) -> list[dict]:
	"""Variants the dealer may order: authorised models, its company's market, priced."""
	from automotive_seed.generator.catalog import AICHI
	from automotive_seed.generator.flows.production import MODEL_LINE

	market = "JP" if company == AICHI else "US"
	price_list = frappe.db.get_value("Customer", dealer, "default_price_list")
	models = frappe.get_all("Dealer Authorised Model", filters={"parent": registration}, pluck="vehicle_model")
	out = []
	for variant in frappe.get_all("Item", filters={"variant_of": ("in", models), "disabled": 0}, pluck="name",
			order_by="name asc"):
		selections = {r.attribute: r.attribute_value for r in frappe.get_all("Item Variant Attribute",
			filters={"parent": variant}, fields=["attribute", "attribute_value"])}
		model = frappe.get_cached_value("Item", variant, "variant_of")
		priced = any(frappe.db.exists("Item Price", {"item_code": code, "price_list": price_list, "selling": 1})
			for code in (variant, model))
		if selections.get("Market") == market and priced and MODEL_LINE.get(model):
			out.append({"variant": variant, "model": model, "selections": selections, "line": MODEL_LINE[model]})
	return out


def _free_in_stock(variant: str) -> int:
	return frappe.db.count("Vehicle Unit", {"variant_item": variant, "quality_status": "Released",
		"inventory_status": "In Stock", "production_status": "Built", "automotive_allocation": ("is", "not set")})


def _waiting_orders(variant: str) -> int:
	return int(flt(frappe.db.sql("""select sum(soi.stock_qty - soi.delivered_qty * soi.conversion_factor
			- ifnull(soi.automotive_allocated_qty, 0))
		from `tabSales Order Item` soi join `tabSales Order` so on so.name = soi.parent
		join `tabCustomer` c on c.name = so.customer
		where so.docstatus = 1 and so.status not in ('Closed', 'Completed', 'On Hold') and c.is_internal_customer = 0
		and soi.item_code = %s
		and soi.stock_qty - soi.delivered_qty * soi.conversion_factor - ifnull(soi.automotive_allocated_qty, 0) > 0""",
		variant)[0][0]))


def _from_stock(variants: list[dict]) -> dict | None:
	for v in variants:
		if _free_in_stock(v["variant"]) > _waiting_orders(v["variant"]):
			return v
	return None


def _build_to_order(variants: list[dict], exclude: set) -> dict | None:
	from automotive_manufacturing.product_engineering.bom_revisions import effective_bom
	from automotive_seed.generator.flows.common import fg_for, line_side_for, store_for

	candidates = []
	for v in variants:
		if v["variant"] in exclude or _free_in_stock(v["variant"]) or _waiting_orders(v["variant"]):
			continue
		bom = effective_bom(v["variant"], nowdate())
		if not bom:
			continue
		line_side = line_side_for(v["line"])
		missing = 0
		for row in frappe.get_all("BOM Item", filters={"parent": bom}, fields=["item_code", "stock_qty"]):
			have = sum(flt(frappe.db.get_value("Bin", {"item_code": row.item_code, "warehouse": w}, "actual_qty"))
				for w in (line_side, store_for(PLANT)))
			missing += have < flt(row.stock_qty)
		candidates.append((missing, v["variant"], v, bom, line_side))
	if not candidates:
		return None
	_missing, _name, choice, bom, line_side = min(candidates, key=lambda c: (c[0], c[1]))
	staged = _stage(bom, line_side)
	return {**choice, "bom": bom, "line_side": line_side, "plant": PLANT,
		"wip_warehouse": frappe.get_cached_value("Plant", PLANT, "wip_warehouse"), "fg_warehouse": fg_for(PLANT),
		"staged": staged}


def _stage(bom: str, line_side: str) -> list[dict]:
	"""One unit of the BOM at the line side through the generator's own material flow, run
	live: the simulated clock starts a quarter of an hour before now, so the staging posts
	before anything the browser journeys post afterwards."""
	from automotive_seed.generator.context import Clock, SeedContext, load_profile
	from automotive_seed.generator.inline_jobs import inline_background_jobs
	from automotive_seed.generator.simulation import Simulation
	from automotive_seed.runner import load_state, save_state

	runs = frappe.get_all("Seed Run", filters={"status": ("in", ("Completed", "Validated"))},
		fields=["name", "profile", "seed"], order_by="modified desc", limit=1)
	if not runs:
		frappe.throw("No finished seed run on this site.")
	run = runs[0]
	spec = load_profile(run.profile)
	spec["seed"] = int(run.seed)
	ctx = SeedContext(run.name, spec)
	state = load_state(run.name)
	ctx.cache["plant_of"] = state.get("plant_of") or {}
	state.setdefault("sim", {}).setdefault("asn_due", {})
	sim = Simulation(ctx, state)
	moment = now_datetime() - dt.timedelta(minutes=15)
	ctx.clock = Clock(moment.date())
	ctx.clock.now = moment
	flags = ("automotive_bulk", "automotive_inline_integration", "mute_emails")
	for flag in flags:
		frappe.flags[flag] = True
	staged = []
	try:
		with inline_background_jobs(), ctx.auto_tick(), ctx.frozen(moment):
			for row in frappe.get_all("BOM Item", filters={"parent": bom}, fields=["item_code", "stock_qty"],
					order_by="idx asc"):
				before = sim.production.actual(row.item_code, line_side)
				sim.production.ensure(row.item_code, flt(row.stock_qty), line_side, PLANT)
				moved = sim.production.actual(row.item_code, line_side) - before
				if moved > 0:
					staged.append({"item_code": row.item_code, "qty": moved})
	except Exception:
		frappe.db.rollback()
		raise
	finally:
		for flag in flags:
			frappe.flags[flag] = False
	frappe.db.commit()
	save_state(run.name, state)
	return staged


def _edi_supplier() -> dict | None:
	"""An EDI supplier (not the portal persona's) with an open order line nothing has been
	announced for; its partner gets a new inbound secret for this test run."""
	persona_supplier = frappe.db.get_value("Partner User Access", {"user": _partner("Supplier")}, "supplier")
	for partner in frappe.get_all("Integration Partner", filters={"party_type": "Supplier", "is_active": 1},
			fields=["name", "party", "message_format", "allowed_message_types"], order_by="name asc"):
		if partner.party == persona_supplier or "asn" not in (partner.allowed_message_types or "").split():
			continue
		line = frappe.db.sql("""select po.name purchase_order, poi.name po_item, poi.item_code, poi.qty
			from `tabPurchase Order` po join `tabPurchase Order Item` poi on poi.parent = po.name
			join `tabItem` i on i.name = poi.item_code
			where po.supplier = %s and po.docstatus = 1 and po.status in ('To Receive and Bill', 'To Receive')
			and poi.received_qty = 0 and i.has_serial_no = 0
			and not exists (select 1 from `tabASN Item` ai join `tabAdvance Shipping Notice` a on a.name = ai.parent
				where ai.po_item = poi.name and a.docstatus = 1)
			order by po.transaction_date desc, po.name, poi.idx limit 1""", partner.party, as_dict=True)
		if not line:
			continue
		secret = frappe.generate_hash(length=48)
		doc = frappe.get_doc("Integration Partner", partner.name)
		doc.inbound_secret = secret
		doc.flags.ignore_permissions = True
		doc.save()
		return {"partner": partner.name, "supplier": partner.party, "format": partner.message_format,
			"secret": secret, "has_batch_no": frappe.get_cached_value("Item", line[0].item_code, "has_batch_no"),
			**line[0]}
	return None
