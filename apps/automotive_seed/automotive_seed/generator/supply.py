"""Supply setup: sourcing awards for production parts, blanket orders, Kanban loops,
transfer price agreements, allocation rules, opening capital, opening stock and FX."""

from __future__ import annotations

import datetime as dt
import math

import frappe
from frappe.utils import add_days, flt, getdate

from automotive_core.governance import approvals
from automotive_seed.generator.catalog import AICHI, KYUSHU, NA
from automotive_seed.generator.context import SeedContext

JIT_PREFIXES = ("TIRE-", "WHEEL-", "BATT12V", "FLUID-", "PAINT-", "PRIMER", "CLEARCOAT", "SEALER", "WELD-WIRE",
	"BUMPER-", "HEADLAMP-", "CARPET-", "HEADLINER-", "DOORTRIM-", "REGULATOR-", "TRIMCOVER-", "PNL-", "FOAM", "IPFRAME-")


def daily_usage(structure, ctx: SeedContext) -> dict[str, float]:
	"""Average daily consumption per purchased item at full rate (for sizing loops and opening stock)."""
	per_day = float(ctx.v("aichi_vehicles_per_day"))
	variants = structure["variants"]
	usage: dict[str, float] = {}

	def explode(bom: str, qty: float) -> None:
		for row in frappe.get_all("BOM Item", filters={"parent": bom}, fields=["item_code", "stock_qty", "bom_no"]):
			sub = row.bom_no or frappe.db.get_value("BOM", {"item": row.item_code, "is_default": 1, "docstatus": 1}, "name")
			if sub and row.item_code not in structure["purchased"]:
				explode(sub, qty * flt(row.stock_qty))
			else:
				usage[row.item_code] = usage.get(row.item_code, 0) + qty * flt(row.stock_qty)

	share = per_day / max(len(variants), 1)
	for _code, info in sorted(variants.items()):
		explode(info["bom"], share)
	return usage


def powertrain_usage(structure, ctx: SeedContext) -> dict[str, float]:
	"""Average daily consumption at Aichi of each powertrain (supplied by Kyushu)."""
	share = float(ctx.v("aichi_vehicles_per_day")) / max(len(structure["variants"]), 1)
	usage: dict[str, float] = {}
	for _code, info in sorted(structure["variants"].items()):
		for row in frappe.get_all("BOM Item", filters={"parent": info["bom"]}, fields=["item_code", "stock_qty"]):
			if row.item_code in structure["powertrains"]:
				usage[row.item_code] = usage.get(row.item_code, 0) + share * flt(row.stock_qty)
	return usage


def sourcing(ctx: SeedContext, structure, roster, suppliers_by_commodity) -> None:
	"""One awarded Sourcing Decision per production part used in a BOM (catalogue parts
	rely on the commodity-level ASL entries created at onboarding)."""
	buyer, manager = roster["Buyer"][0], roster["Procurement Manager"][0]
	rng = ctx.rng("sourcing")
	used = {r.item_code for r in frappe.get_all("BOM Item", fields=["item_code"], distinct=True)}
	for code in sorted(used & set(structure["purchased"])):
		info = structure["purchased"][code]
		candidates = suppliers_by_commodity.get(info["commodity"]) or []
		if not candidates:
			raise RuntimeError(f"no supplier covers commodity {info['commodity']} ({code})")
		plant = info["plant"]
		company = KYUSHU if plant == "KYUSHU-PT" else AICHI
		if frappe.db.exists("Sourcing Decision", {"item_code": code, "plant": plant, "docstatus": 1}):
			continue
		chosen = rng.sample(candidates, min(2, len(candidates)))
		quotes = []
		for i, supplier in enumerate(chosen):
			quotes.append({"supplier": supplier, "unit_price": round(info["cost"] * rng.uniform(0.96, 1.05), 2),
				"lead_time_days": max(2, info["lead"] + rng.randint(-1, 2)), "quality_score": rng.randint(84, 99),
				"awarded_share_percent": (80 if i == 0 else 20) if len(chosen) > 1 else 100})
		with ctx.as_user(buyer):
			doc = frappe.get_doc({"doctype": "Sourcing Decision", "company": company, "plant": plant, "item_code": code,
				"effective_from": ctx.start.isoformat(), "method": "Best Value",
				"rationale": "Annual sourcing round: weighted price / lead time / quality", "quotes": quotes})
			doc.insert()
			req = approvals.request_approval(doc.doctype, doc.name)
		with ctx.as_user(manager):
			approvals.decide(req["name"], "Approve")
		with ctx.as_user(buyer):
			doc.reload()
			doc.submit()
		ctx.count("Sourcing Decision")
	frappe.db.commit()


def primary_supplier(item_code: str, plant: str) -> str | None:
	from automotive_supply_chain.supplier_collaboration.sourcing import approved_entries

	rows = approved_entries(item_code, plant)
	return rows[0].supplier if rows else None


def blanket_orders_and_kanban(ctx: SeedContext, structure, roster) -> dict[str, str]:
	"""JIT parts at Aichi: supplier Kanban loops to the consuming line side, released against
	six-month blanket orders. Returns {item: loop}."""
	usage = daily_usage(structure, ctx)
	buyer = roster["Buyer"][0]
	lines_for_item = _consuming_lines(structure)
	by_supplier: dict[str, list[tuple[str, float]]] = {}
	for code in sorted(usage):
		if code not in structure["purchased"] or not code.startswith(JIT_PREFIXES):
			continue
		if structure["purchased"][code]["inspect"]:
			continue
		supplier = primary_supplier(code, "AICHI-VA")
		if supplier:
			by_supplier.setdefault(supplier, []).append((code, usage[code]))
	loops = {}
	for supplier, items in sorted(by_supplier.items()):
		bo_name = frappe.db.get_value("Blanket Order", {"supplier": supplier, "company": AICHI, "docstatus": 1}, "name")
		if not bo_name:
			with ctx.as_user(buyer):
				bo = frappe.get_doc({"doctype": "Blanket Order", "blanket_order_type": "Purchasing", "supplier": supplier,
					"company": AICHI, "from_date": add_days(ctx.start, -1), "to_date": add_days(ctx.end, 60),
					"items": [{"item_code": code, "qty": math.ceil(u * 400), "rate": _sourced_price(code, supplier)}
						for code, u in items]})
				bo.insert()
				bo.submit()
			bo_name = bo.name
			ctx.count("Blanket Order")
		for code, u in items:
			for line in lines_for_item.get(code, []):
				loop_code = f"KL-{line}-{code}"[:120]
				if frappe.db.exists("Kanban Loop", loop_code):
					loops[code] = loop_code
					continue
				line_side = frappe.db.get_value("Production Line", line, "line_side_warehouse")
				share = u / max(len(lines_for_item[code]), 1)
				# half a day's demand per container, in whole units unless the UOM is divisible
				container = share * 0.5 if share >= 2 else share
				uom = frappe.get_cached_value("Item", code, "stock_uom")
				if frappe.get_cached_value("UOM", uom, "must_be_whole_number"):
					container = max(1, math.ceil(container))
				else:
					container = max(0.1, round(container, 2))
				frappe.get_doc({"doctype": "Kanban Loop", "loop_code": loop_code, "item_code": code, "plant": "AICHI-VA",
					"production_line": line, "supply_type": "Supplier", "supplier": supplier, "blanket_order": bo_name,
					"line_side_warehouse": line_side, "container_qty": container,
					"number_of_cards": max(4, math.ceil(share * 3 / container)), "lead_time_hours": 18,
					"daily_demand": share, "safety_cards": 1,
					"delivery_windows": [{"weekday": d, "window_start": "06:00:00", "window_end": "08:00:00"}
						for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")]}).insert(
					ignore_permissions=True)
				ctx.count("Kanban Loop")
				loops[code] = loop_code
	frappe.db.commit()
	return loops


def _sourced_price(code: str, supplier: str) -> float:
	price = frappe.db.get_value("Item Price", {"item_code": code, "supplier": supplier, "buying": 1}, "price_list_rate")
	return flt(price) or flt(frappe.get_cached_value("Item", code, "automotive_standard_cost"))


def _consuming_lines(structure) -> dict[str, list[str]]:
	"""Item -> lines whose line-side warehouse it is consumed from (via vehicle or module BOMs)."""
	out: dict[str, set[str]] = {}
	for info in structure["modules"].values():
		if info["plant"] != "AICHI-VA":
			continue
		for row in frappe.get_all("BOM Item", filters={"parent": info["bom"]}, pluck="item_code"):
			out.setdefault(row, set()).add(info["line"])
	for info in structure["variants"].values():
		for row in frappe.get_all("BOM Item", filters={"parent": info["bom"]}, pluck="item_code"):
			out.setdefault(row, set()).add(info["line"])
	return {k: sorted(v) for k, v in out.items()}


def transfer_prices(ctx: SeedContext, roster) -> None:
	controller = roster["Group Controller"][0]
	agreements = [
		{"seller_company": KYUSHU, "buyer_company": AICHI, "item_group": "Powertrain Assemblies", "pricing_method":
			"Cost Plus", "markup_percent": 8, "currency": "JPY", "basis": "Cost plus 8% (benchmark study FY2025)"},
		{"seller_company": AICHI, "buyer_company": NA, "item_group": "Vehicles", "pricing_method": "Cost Plus",
			"markup_percent": 6, "currency": "USD", "basis": "Cost plus 6% (limited-risk distributor)"},
	]
	for values in agreements:
		if frappe.db.exists("Transfer Price Agreement", {"seller_company": values["seller_company"],
				"buyer_company": values["buyer_company"], "docstatus": 1}):
			continue
		tpa = frappe.get_doc({"doctype": "Transfer Price Agreement", "valid_from": add_days(ctx.start, -1), **values})
		tpa.insert(ignore_permissions=True)
		req = approvals.request_approval(tpa.doctype, tpa.name)
		with ctx.as_user(controller):
			approvals.decide(req["name"], "Approve")
		tpa.reload()
		tpa.submit()
		ctx.count("Transfer Price Agreement")
	for company, method in ((AICHI, "Dealer Priority"), (NA, "Order Date (FIFO)")):
		rule = f"{company[:20]} default allocation"
		if not frappe.db.exists("Allocation Rule", rule):
			frappe.get_doc({"doctype": "Allocation Rule", "rule_name": rule, "company": company, "priority": 10,
				"method": method, "is_active": 1}).insert(ignore_permissions=True)
	frappe.db.commit()


def exchange_rate(ctx: SeedContext, day: dt.date) -> float:
	"""Synthetic USD/JPY random walk with mean reversion (deterministic per day)."""
	key = "fx-path"
	path = ctx.cache.setdefault(key, {})
	if day in path:
		return path[day]
	previous = path.get(add_days(day, -1)) or 149.8
	rng = ctx.rng("fx", day)
	value = round(previous + (150 - previous) * 0.02 + rng.gauss(0, 0.45), 4)
	path[day] = value
	for from_c, to_c, rate in (("USD", "JPY", value), ("JPY", "USD", round(1 / value, 9))):
		if not frappe.db.exists("Currency Exchange", {"date": day, "from_currency": from_c, "to_currency": to_c}):
			frappe.get_doc({"doctype": "Currency Exchange", "date": day, "from_currency": from_c, "to_currency": to_c,
				"exchange_rate": rate, "for_buying": 1, "for_selling": 1}).insert(ignore_permissions=True)
	return value


def capital_account(company: str) -> str:
	return frappe.db.get_value("Account", {"company": company, "account_name": "Capital Stock", "is_group": 0}, "name")


def storage(ctx: SeedContext, structure, loops: dict) -> str:
	"""Forward stores and reserve storage: put-away and reorder rules for every stored part
	(purchased, not delivered just in time to the line, not inspected on receipt), sized from
	its daily usage: three days in the forward store, replenished at a day and a half.
	Runs before the opening stock (a rule's capacity may not be below the stock on hand)."""
	from automotive_supply_chain.logistics.storage import set_storage

	stored = 0
	for code, per_day in sorted(daily_usage(structure, ctx).items()):
		info = structure["purchased"].get(code)
		if not info or info["inspect"] or code in loops or per_day <= 0:
			continue
		if info["plant"] == "AICHI-VA" and code.startswith(JIT_PREFIXES):
			continue
		capacity = max(math.ceil(per_day * 3), 3)
		level = max(math.ceil(per_day * 1.5), 1)
		set_storage(code, info["plant"], capacity, level, capacity - level)
		stored += 1
	frappe.db.commit()
	return f"{stored} stored parts"


def opening(ctx: SeedContext, structure, roster) -> None:
	"""Opening capital (bank) per company and opening stock (Stock Reconciliation) the day before history starts."""
	from automotive_seed.generator.flows.common import company_bank_account

	day = add_days(ctx.start, -1)
	with ctx.frozen(dt.datetime.combine(day, dt.time(9, 0))):
		exchange_rate(ctx, day)
		for company, amount in ((AICHI, 30_000_000_000), (KYUSHU, 8_000_000_000), (NA, 120_000_000)):
			if frappe.db.exists("Journal Entry", {"company": company, "voucher_type": "Opening Entry", "docstatus": 1}):
				continue
			bank = company_bank_account(company)
			equity = capital_account(company)
			je = frappe.get_doc({"doctype": "Journal Entry", "voucher_type": "Opening Entry", "company": company,
				"posting_date": day, "is_opening": "Yes", "accounts": [
					{"account": bank, "debit_in_account_currency": amount},
					{"account": equity, "credit_in_account_currency": amount}]})
			je.insert(ignore_permissions=True)
			je.submit()
			ctx.count("Journal Entry")
		usage = daily_usage(structure, ctx)
		_opening_stock(ctx, structure, usage, day)
	frappe.db.commit()


def _opening_stock(ctx, structure, usage, day) -> None:
	from automotive_seed.generator.flows.common import line_side_for, store_for

	by_company: dict[str, list[dict]] = {}
	lines = _consuming_lines(structure)
	for code, per_day in sorted(usage.items()):
		info = structure["purchased"].get(code)
		if not info:
			continue
		company = KYUSHU if info["plant"] == "KYUSHU-PT" else AICHI
		days = 4
		if info["plant"] == "AICHI-VA":
			targets = [line_side_for(line) for line in lines.get(code, [])] or [store_for("AICHI-VA")]
		else:
			targets = [store_for("KYUSHU-PT")]
		supplier = primary_supplier(code, info["plant"])
		for n, warehouse in enumerate(targets):
			qty = math.ceil(per_day * days / len(targets))
			row = {"item_code": code, "warehouse": warehouse, "qty": qty, "valuation_rate": info["cost"],
				"use_serial_batch_fields": 1}
			if info["tracking"] == "batch":
				batch_id = f"OPEN-{code}-{n + 1}"[:140]
				if not frappe.db.exists("Batch", batch_id):
					frappe.get_doc({"doctype": "Batch", "batch_id": batch_id, "item": code, "automotive_supplier": supplier,
						"automotive_supplier_lot": "OPENING"}).insert(ignore_permissions=True)
				row["batch_no"] = batch_id
			elif info["tracking"] == "serial":
				row["qty"] = math.ceil(qty)
				row["serial_no"] = "\n".join(f"{code}-OPN{n + 1}-{i:05d}" for i in range(1, row["qty"] + 1))
			by_company.setdefault(company, []).append(row)
	# Aichi starts with a few days of Kyushu powertrains (at the cost-plus transfer price), as
	# a running plant would; afterwards they arrive by intercompany shipment.
	for code, per_day in sorted(powertrain_usage(structure, ctx).items()):
		qty = math.ceil(per_day * 4)
		cost = round(flt(frappe.get_cached_value("Item", code, "automotive_standard_cost")) * 1.08, 2)
		by_company.setdefault(AICHI, []).append({"item_code": code, "warehouse": store_for("AICHI-VA"), "qty": qty,
			"valuation_rate": cost, "use_serial_batch_fields": 1,
			"serial_no": "\n".join(f"{code.replace('-', '')}-OPN-{i:05d}" for i in range(1, qty + 1))})
	for company, rows in by_company.items():
		if frappe.db.exists("Stock Reconciliation", {"company": company, "purpose": "Opening Stock", "docstatus": 1}):
			continue
		temporary = frappe.db.get_value("Account", {"company": company, "account_type": "Temporary", "is_group": 0},
			"name")
		recon = frappe.get_doc({"doctype": "Stock Reconciliation", "company": company, "purpose": "Opening Stock",
			"posting_date": day, "set_posting_time": 1, "posting_time": "09:00:00", "expense_account": temporary,
			"items": rows})
		recon.insert(ignore_permissions=True)
		recon.submit()
		ctx.count("Stock Reconciliation")
		_close_temporary_opening(ctx, company, temporary, day)


def _close_temporary_opening(ctx, company: str, temporary: str, day) -> None:
	"""Opening stock is contributed capital: move the temporary-opening balance to equity."""
	balance = flt(frappe.db.sql("""select sum(debit - credit) from `tabGL Entry` where company=%s and account=%s
		and is_cancelled=0""", (company, temporary))[0][0])
	if abs(balance) < 0.005:
		return
	equity = capital_account(company)
	amount = abs(balance)
	# a credit balance (stock debited, temporary credited) is cleared by debiting it against equity
	temp_side, equity_side = (("debit", "credit") if balance < 0 else ("credit", "debit"))
	je = frappe.get_doc({"doctype": "Journal Entry", "voucher_type": "Opening Entry", "company": company,
		"posting_date": day, "is_opening": "Yes", "user_remark": "Opening stock contributed as capital",
		"accounts": [
			{"account": temporary, f"{temp_side}_in_account_currency": amount},
			{"account": equity, f"{equity_side}_in_account_currency": amount},
		]})
	je.insert(ignore_permissions=True)
	je.submit()
	ctx.count("Journal Entry")
