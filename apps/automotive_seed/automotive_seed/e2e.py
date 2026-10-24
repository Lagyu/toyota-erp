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

import contextlib
import datetime as dt
import functools
import json
import os
import traceback

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
	"kyushu_logistics": ("Logistics Coordinator", "KYUSHU-PT"),
}
PLANT = "AICHI-VA"


def _cli(fn):
	"""``bench execute`` swallows a function's exception and retries the path with ``eval``,
	reporting a misleading NameError: print the real traceback and exit non-zero instead."""
	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		try:
			return fn(*args, **kwargs)
		except Exception:
			traceback.print_exc()
			raise SystemExit(1) from None

	return wrapper


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


@_cli
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
	# second approvers: payments need two treasury approvers, dispositions a second quality manager
	for key, role in (("treasury", "Treasury Approver"), ("quality_manager", "Quality Manager")):
		others = frappe.db.sql("""select u.name from `tabUser` u join `tabHas Role` r on r.parent = u.name
			where r.role = %s and u.enabled = 1 and u.user_type = 'System User' and u.name != %s
			and u.name not in ('Administrator', 'Guest') order by u.name""",
			(role, users.get(key) or ""), pluck=True)
		if others:
			users[f"{key}_2"] = others[0]
	# a Japanese-speaking planner (ERP-12.3): the other personas keep the English desk the specs read
	planners = frappe.db.sql("""select u.name from `tabUser` u join `tabHas Role` r on r.parent = u.name
		where r.role = 'Production Planner' and u.enabled = 1 and u.user_type = 'System User' and u.name != %s
		and u.name not in ('Administrator', 'Guest') order by u.name""", users.get("planner") or "", pluck=True)
	if planners:
		users["planner_ja"] = planners[0]
		frappe.db.set_value("User", planners[0], "language", "ja")
	# Administrator holds every role but is no persona: its password is the stack's own
	assert "Administrator" not in users.values(), users
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


@_cli
def prepare_journeys(output: str | None = None) -> str | dict:
	"""Preconditions for the browser journeys, arranged at the current time through the
	dataset's own flows (never direct ledger writes):

	- ``j01`` build to order: a variant the e2e dealer may order with no vehicle waiting in
	  stock and no other order waiting for one, and one unit of its components staged at
	  the line side the way the generator stages a day's builds (store transfers, expedited
	  supplier deliveries with incoming inspection);
	- ``j09`` allocation from stock: a variant the dealer may order with a released vehicle
	  free in stock and no order waiting for it;
	- ``j04`` engineering change: a released module BOM with a whole-unit component, no change
	  pending, and a work order already built on it (history that must stay pinned);
	- ``j05`` defective batch: a supplier's batch in use at an Aichi line (stock at the line
	  side), not on hold, with the receipt it came on;
	- ``j08`` traceability: a vehicle delivered to a dealer whose genealogy holds a serialised
	  component and a supplier batch at least two levels down;
	- ``j06`` invoice matching: a receipt nobody has invoiced yet, from a supplier whose bank
	  account is verified (payments go only to verified accounts);
	- ``j07`` intercompany supply: a powertrain Kyushu has in stock and an agreed transfer
	  price to Aichi;
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
	fixtures["j06"] = _receipt_to_invoice(company)
	fixtures["j07"] = _intercompany_supply()
	fixtures["j04"] = _engineering_change()
	fixtures["j05"] = _batch_in_use()
	fixtures["j08"] = _traced_vehicle()
	frappe.db.commit()
	if output:
		_write(output, json.dumps(fixtures, sort_keys=True, default=str))
		staged = len((fixtures["j01"] or {}).get("staged") or [])
		return (f"journeys: j01 {(fixtures['j01'] or {}).get('variant')} ({staged} components staged), "
			f"j04 {(fixtures['j04'] or {}).get('bom')}, j05 {(fixtures['j05'] or {}).get('batch_no')}, j06 {(fixtures['j06'] or {}).get('purchase_receipt')}, "
			f"j07 {(fixtures['j07'] or {}).get('item_code')}, j08 {(fixtures['j08'] or {}).get('vin')}, "
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


def _live_clock(start: dt.datetime):
	from automotive_seed.generator.context import Clock

	class LiveClock(Clock):
		"""Strictly increasing by 10 ms per step and never jumping to an hour of the day: an
		arrangement of hundreds of documents spans seconds, not simulated hours."""

		def at(self, hour: int, minute: int = 0) -> dt.datetime:
			return self.tick()

		def tick(self, seconds: float = 30) -> dt.datetime:
			self.now += dt.timedelta(milliseconds=10)
			return self.now

	clock = LiveClock(start.date())
	clock.now = start
	return clock


@contextlib.contextmanager
def _live():
	"""The generator's simulation run live, for arrangement only: its people and flows, just
	before the current time (a quarter of an hour back, but after every stock posting so
	far), so arrangements land before anything the browser journeys post afterwards. All
	or nothing: a failure rolls the session back."""
	from automotive_seed.generator.context import SeedContext, load_profile
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
	latest = frappe.db.sql("select max(posting_datetime) from `tabStock Ledger Entry`")[0][0]
	moment = now_datetime() - dt.timedelta(minutes=15)
	if latest and latest >= moment:
		moment = latest + dt.timedelta(seconds=1)
	ctx.clock = _live_clock(moment)
	flags = ("automotive_bulk", "automotive_inline_integration", "mute_emails")
	for flag in flags:
		frappe.flags[flag] = True
	try:
		with inline_background_jobs(), ctx.auto_tick(), ctx.frozen(moment):
			yield sim, ctx
	except Exception:
		frappe.db.rollback()
		raise
	finally:
		for flag in flags:
			frappe.flags[flag] = False
	frappe.db.commit()
	save_state(run.name, state)


def _stage(bom: str, line_side: str) -> list[dict]:
	"""One unit of the BOM at the line side, as the generator stages a day's builds."""
	staged = []
	with _live() as (sim, _ctx):
		for row in frappe.get_all("BOM Item", filters={"parent": bom}, fields=["item_code", "stock_qty"],
				order_by="idx asc"):
			before = sim.production.actual(row.item_code, line_side)
			sim.production.ensure(row.item_code, flt(row.stock_qty), line_side, PLANT)
			moved = sim.production.actual(row.item_code, line_side) - before
			if moved > 0:
				staged.append({"item_code": row.item_code, "qty": moved})
	return staged


def _build_powertrain(code: str) -> None:
	"""Kyushu builds and inspects one powertrain, as on a production day (sub-assemblies and
	material included)."""
	from automotive_seed.generator.catalog import KYUSHU
	from automotive_seed.generator.flows.common import fg_for, store_for

	with _live() as (sim, ctx):
		production = sim.production
		planner = sim.person("Production Planner", "KYUSHU-PT")
		supervisor = sim.person("Production Supervisor", "KYUSHU-PT")
		if code.startswith("P180"):
			production._engines(1, planner, supervisor)
		else:
			production._machined("GEARSET-240", 2 if code == "E240-D" else 1, planner, supervisor)
			production._machined("HOUSING-240", 1, planner, supervisor)
		wo = production.work_order(code, 1, KYUSHU, "KYUSHU-PT", production.structure["powertrains"][code]["line"],
			store_for("KYUSHU-PT"), fg_for("KYUSHU-PT"), ctx.clock.tick(60), planner)
		se = production.build(wo, 1, supervisor)
		sim.quality.powertrain_inspection(code, se, sim.person("Quality Inspector", "KYUSHU-PT"))


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


def _receipt_to_invoice(company: str) -> dict | None:
	from automotive_seed.generator.flows.common import company_bank_account

	rows = frappe.db.sql("""select pr.name, pr.supplier from `tabPurchase Receipt` pr
		join `tabSupplier` s on s.name = pr.supplier
		where pr.docstatus = 1 and pr.is_return = 0 and pr.per_billed = 0 and pr.company = %s
		and s.is_internal_supplier = 0 and ifnull(pr.supplier_delivery_note, '') not like 'CSM-%%'
		and exists (select 1 from `tabBank Account` ba where ba.party_type = 'Supplier' and ba.party = pr.supplier
			and ba.automotive_verification_status = 'Verified')
		order by pr.posting_date asc, pr.name asc limit 1""", company, as_dict=True)
	if not rows:
		return None
	return {"purchase_receipt": rows[0].name, "supplier": rows[0].supplier, "company": company,
		"paid_from": company_bank_account(company),
		"bank_account": frappe.db.get_value("Bank Account", {"party_type": "Supplier", "party": rows[0].supplier,
			"automotive_verification_status": "Verified"}, "name")}


def _intercompany_supply() -> dict | None:
	"""A powertrain Kyushu holds (built here when its finished store is empty, since the
	dataset ships everything it builds) with an agreed transfer price to Aichi."""
	from automotive_seed.generator.catalog import AICHI, KYUSHU
	from automotive_seed.generator.flows.common import fg_for, store_for
	from automotive_supply_chain.intercompany.transfer_pricing import agreed_price

	seller_store = fg_for("KYUSHU-PT")

	def in_stock() -> list[str]:
		return frappe.db.sql_list("""select item_code from `tabSerial No` where warehouse = %s and status = 'Active'
			group by item_code order by count(*) desc, item_code asc""", seller_store)

	codes = in_stock()
	if not codes:
		priced = [c for c in ("P180-FWD", "P180-AWD", "E240-S", "E240-D") if frappe.db.exists("Item", c)
			and agreed_price(KYUSHU, AICHI, c, nowdate())]
		if not priced:
			return None
		_build_powertrain(priced[0])
		codes = in_stock()
	for item_code in codes:
		price = agreed_price(KYUSHU, AICHI, item_code, nowdate())
		if price:
			return {"item_code": item_code, "rate": price[0], "seller": KYUSHU, "buyer": AICHI,
				"supplier": frappe.db.get_value("Supplier", {"is_internal_supplier": 1, "represents_company": KYUSHU}),
				"seller_warehouse": seller_store, "buyer_warehouse": store_for(PLANT)}
	return None


def _engineering_change() -> dict | None:
	boms = frappe.db.sql("""select b.name, b.item from `tabBOM` b join `tabItem` i on i.name = b.item
		where b.docstatus = 1 and b.is_active = 1 and ifnull(b.automotive_superseded_by, '') = ''
		and i.item_group = 'Vehicle Modules'
		and exists (select 1 from `tabWork Order` wo where wo.bom_no = b.name and wo.docstatus = 1)
		and not exists (select 1 from `tabECO Change` c join `tabEngineering Change Order` e on e.name = c.parent
			where c.target_bom = b.name and e.docstatus < 2)
		order by b.item asc""", as_dict=True)
	for bom in boms:
		for line in frappe.get_all("BOM Item", filters={"parent": bom.name}, fields=["item_code", "qty", "stock_uom"],
				order_by="idx asc"):
			if frappe.get_cached_value("UOM", line.stock_uom, "must_be_whole_number"):
				return {"item": bom.item, "bom": bom.name, "component": line.item_code, "qty": line.qty,
					"new_qty": line.qty + 1, "work_order": frappe.db.get_value("Work Order",
						{"bom_no": bom.name, "docstatus": 1}, "name", order_by="creation desc")}
	return None


def _batch_in_use() -> dict | None:
	from erpnext.stock.doctype.batch.batch import get_batch_qty

	from automotive_seed.generator.flows.common import line_side_for, store_for

	plant = frappe.get_cached_doc("Plant", PLANT)
	for line in ("AICHI-FA1", "AICHI-FA2"):
		line_side = line_side_for(line)
		for item_code in frappe.get_all("Bin", filters={"warehouse": line_side, "actual_qty": (">", 0)},
				pluck="item_code", order_by="item_code asc"):
			item = frappe.get_cached_value("Item", item_code, ["has_batch_no", "is_purchase_item", "has_serial_no"],
				as_dict=True)
			if not item.has_batch_no or not item.is_purchase_item or item.has_serial_no:
				continue
			batches = sorted((b for b in get_batch_qty(warehouse=line_side, item_code=item_code) or []
				if flt(b.get("qty")) >= 2), key=lambda b: (-flt(b["qty"]), b["batch_no"]))
			for b in batches:
				supplier = frappe.db.get_value("Batch", b["batch_no"], "automotive_supplier")
				receipt = frappe.db.get_value("Purchase Receipt Item", {"batch_no": b["batch_no"], "docstatus": 1},
					["parent", "name"], as_dict=True)
				held = frappe.db.exists("Quality Hold", {"batch_no": b["batch_no"], "status": "Active"})
				if supplier and receipt and not held:
					station = frappe.get_all("Production Line Station", filters={"parent": line}, pluck="workstation",
						order_by="idx asc", limit=1)
					return {"item_code": item_code, "batch_no": b["batch_no"], "supplier": supplier, "line": line,
						"workstation": station[0] if station else frappe.get_cached_value("Production Line", line,
							"default_workstation"),
						"line_side": line_side, "store": store_for(PLANT), "quarantine": plant.quarantine_warehouse,
						"line_side_qty": flt(b["qty"]), "purchase_receipt": receipt.parent, "receipt_row": receipt.name,
						"company": plant.company}
	return None


def _traced_vehicle() -> dict | None:
	from automotive_manufacturing.traceability.genealogy import components_of

	for vin in frappe.get_all("Vehicle Unit", filters={"production_status": "Built", "inventory_status": "Delivered",
			"dealer": ("is", "set")}, pluck="name", order_by="built_at desc, name asc", limit=40):
		tree = components_of(vin)
		deep = [r for r in tree if r["level"] >= 2 and r["component_batch_no"] and r.get("supplier")]
		serial = [r for r in tree if r["level"] == 1 and r["component_serial_no"]]
		if deep and serial:
			return {"vin": vin, "dealer": frappe.db.get_value("Vehicle Unit", vin, "dealer"),
				"batch_item": deep[0]["component_item"], "batch_no": deep[0]["component_batch_no"],
				"batch_supplier": deep[0]["supplier"], "batch_level": deep[0]["level"],
				"serial_item": serial[0]["component_item"], "serial_no": serial[0]["component_serial_no"]}
	return None
