"""People and partners through the real workflows: internal accounts via approved User
Provisioning Requests, employees and plant assignments; suppliers via Supplier
Onboarding (approved), dealers via Dealer Registration (approved); carriers; verified
supplier bank accounts; integration partners for EDI suppliers/dealers."""

from __future__ import annotations

import zlib

import frappe
from frappe.utils import add_days

from automotive_core.governance import approvals
from automotive_seed.generator import names
from automotive_seed.generator.catalog import AICHI, KYUSHU, NA
from automotive_seed.generator.context import SeedContext

PLANT_COMPANY = {"AICHI-VA": AICHI, "KYUSHU-PT": KYUSHU, "NA-PDC": NA}
PLANT_ROLE = {
	"Plant Director": "Director",
	"Production Planner": "Planner",
	"Production Supervisor": "Supervisor",
	"Line Operator": "Operator",
	"Warehouse Clerk": "Warehouse",
	"Quality Inspector": "Quality",
	"Quality Manager": "Quality",
	"Product Engineer": "Engineering",
	"Engineering Manager": "Engineering",
	"Logistics Coordinator": "Logistics",
	"Sales Manager": "Administration",
	"Dealer Liaison": "Administration",
}
COMMODITIES = [
	"stamping",
	"tires",
	"glass",
	"electronics",
	"lighting",
	"interior",
	"seating",
	"motors",
	"plastics",
	"harness",
	"battery",
	"chemicals",
	"paint",
	"castings",
	"forgings",
	"machining",
	"rubber",
	"fasteners",
	"steel",
]
COMMODITY_GROUPS = {
	"stamping": ["Body & Exterior"],
	"tires": ["Chassis & Wheels"],
	"glass": ["Body & Exterior"],
	"electronics": ["Electrical & Electronics", "Service Parts"],
	"lighting": ["Electrical & Electronics"],
	"interior": ["Interior", "Service Parts"],
	"seating": ["Interior"],
	"motors": ["Electrical & Electronics", "Powertrain Components"],
	"plastics": ["Interior", "Body & Exterior", "Service Parts"],
	"harness": ["Electrical & Electronics"],
	"battery": ["Electrical & Electronics", "Powertrain Components"],
	"chemicals": ["Fluids & Paint"],
	"paint": ["Fluids & Paint"],
	"castings": ["Castings & Forgings"],
	"forgings": ["Castings & Forgings", "Powertrain Components"],
	"machining": ["Powertrain Components"],
	"rubber": ["Powertrain Components", "Body & Exterior"],
	"fasteners": ["Fasteners & Hardware"],
	"steel": ["Chassis & Wheels"],
}


def _email(first: str, last: str, n: int, domain: str = "hinode.example") -> str:
	return f"{first}.{last}.{n}@{domain}".lower()


def _create_user_directly(email: str, first: str, last: str, profile: str) -> str:
	"""Only the very first IT operators are created directly (they approve all later accounts)."""
	if not frappe.db.exists("User", email):
		user = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first,
				"last_name": last,
				"send_welcome_email": 0,
				"user_type": "System User",
				"role_profiles": [{"role_profile": profile}],
			}
		)
		user.flags.ignore_permissions = True
		user.insert()
	return email


def _provision(
	ctx: SeedContext,
	maker: str,
	checker: str,
	email: str,
	first: str,
	last: str,
	profile: str,
	scopes: list[tuple[str, str]],
) -> str:
	if frappe.db.exists("User", email):
		return email
	with ctx.as_user(maker):
		req = frappe.get_doc(
			{
				"doctype": "User Provisioning Request",
				"request_type": "Create",
				"email": email,
				"first_name": first,
				"last_name": last,
				"user_type": "System User",
				"role_profile": profile,
				"justification": f"Joiner: {profile}",
				"scopes": [
					{"allow": a, "for_value": v, "is_default": i == 0} for i, (a, v) in enumerate(scopes)
				],
			}
		).insert()
		request = approvals.request_approval(req.doctype, req.name)
	with ctx.as_user(checker):
		approvals.decide(request["name"], "Approve")
	with ctx.as_user(maker):
		req.reload()
		req.submit()
	ctx.count("User")
	return email


def people(ctx: SeedContext) -> dict[str, list[str]]:
	rng = ctx.rng("people")
	roster: dict[str, list[str]] = {}
	it_users = []
	for i in range(2):
		first, last = names.person(rng, "JP")
		it_users.append(_create_user_directly(_email(first, last, i + 1), first, last, "IT Operator"))
	roster["IT Operator"] = it_users
	seq = 10
	plan: list[tuple[str, str, str]] = []  # (profile, plant or None, region)
	for profile, n in ctx.profile["people"]["group"].items():
		plan += [(profile, None, "JP")] * (n - (2 if profile == "IT Operator" else 0))
	for plant in ("AICHI-VA", "KYUSHU-PT"):
		for profile, n in ctx.profile["people"]["per_manufacturing_plant"].items():
			plan += [(profile, plant, "JP")] * n
	for profile, n in ctx.profile["people"]["na_plant"].items():
		plan += [(profile, "NA-PDC", "US")] * n
	for profile, plant, region in plan:
		seq += 1
		first, last = names.person(rng, region)
		email = _email(first, last, seq)
		scopes = []
		if plant:
			scopes.append(("Plant", plant))
			scopes.append(("Company", PLANT_COMPANY[plant]))
		_provision(ctx, it_users[0], it_users[1], email, first, last, profile, scopes)
		roster.setdefault(profile, []).append(email)
		ctx.cache.setdefault("plant_of", {})[email] = plant
	frappe.db.commit()
	_employees(ctx, roster)
	return roster


def _employees(ctx: SeedContext, roster: dict[str, list[str]]) -> None:
	plant_of = ctx.cache.get("plant_of", {})
	for profile, users in roster.items():
		for email in users:
			plant = plant_of.get(email)
			company = PLANT_COMPANY.get(plant, AICHI)
			if frappe.db.exists("Employee", {"user_id": email}):
				continue
			user = frappe.db.get_value("User", email, ["first_name", "last_name"], as_dict=True)
			emp = frappe.get_doc(
				{
					"doctype": "Employee",
					"first_name": user.first_name,
					"last_name": user.last_name,
					"gender": "Prefer not to say"
					if frappe.db.exists("Gender", "Prefer not to say")
					else frappe.db.get_value("Gender", {}, "name", order_by="name asc"),
					"date_of_birth": "1985-04-01",
					"date_of_joining": "2020-04-01",
					"company": company,
					"user_id": email,
					"status": "Active",
					# row-level scope comes from provisioning scopes and Plant Assignments, not ERPNext's
					# automatic "own company" User Permission (which would confine group roles to one company)
					"create_user_permission": 0,
				}
			)
			emp.flags.ignore_permissions = True
			emp.insert()
			ctx.count("Employee")
			if plant:
				line = None
				if profile in ("Line Operator", "Production Supervisor") and plant != "NA-PDC":
					lines = frappe.get_all(
						"Production Line", filters={"plant": plant}, pluck="name", order_by="name"
					)
					line = lines[zlib.crc32(email.encode()) % len(lines)] if lines else None
				frappe.get_doc(
					{
						"doctype": "Plant Assignment",
						"employee": emp.name,
						"plant": plant,
						"production_line": line,
						"assignment_role": PLANT_ROLE.get(profile, "Administration"),
						"from_date": "2024-04-01",
						"grant_plant_access": 1,
					}
				).insert(ignore_permissions=True)
				ctx.count("Plant Assignment")
	frappe.db.commit()


def _portal_user(email: str, first: str, last: str) -> str:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": first,
				"last_name": last,
				"user_type": "Website User",
				"send_welcome_email": 0,
			}
		).insert(ignore_permissions=True)
	return email


def suppliers(ctx: SeedContext, roster: dict[str, list[str]]) -> dict[str, list[str]]:
	rng = ctx.rng("suppliers")
	buyer, managers, quality = roster["Buyer"][0], roster["Procurement Manager"], roster["Quality Manager"]
	n = int(ctx.profile["partners"]["suppliers"])
	by_commodity: dict[str, list[str]] = {c: [] for c in COMMODITIES}
	for i in range(n):
		# with fewer suppliers than commodities each supplier covers several, so every
		# commodity has a source; otherwise one commodity each, round robin
		covered = (
			[COMMODITIES[k] for k in range(len(COMMODITIES)) if k % n == i]
			if n < len(COMMODITIES)
			else [COMMODITIES[i % len(COMMODITIES)]]
		)
		commodity = covered[0]
		region = "US" if i % 15 == 14 else "JP"
		name = names.supplier_name(i, commodity, region)
		code = f"S{i + 1:03d}"
		if not frappe.db.exists("Supplier", {"supplier_name": name}):
			first, last = names.person(rng, region)
			contacts = [
				{
					"first_name": first,
					"last_name": last,
					"email": f"sales@{code.lower()}.supplier.example",
					"partner_role": "Administrator",
				}
			]
			if rng.random() < float(ctx.profile["partners"]["second_portal_user_share"]):
				f2, l2 = names.person(rng, region)
				contacts.append(
					{
						"first_name": f2,
						"last_name": l2,
						"email": f"logistics@{code.lower()}.supplier.example",
						"partner_role": "Logistics",
					}
				)
			plants = sorted({p for c in covered for p in _commodity_plants(c)})
			with ctx.as_user(buyer):
				doc = frappe.get_doc(
					{
						"doctype": "Supplier Onboarding",
						"supplier_name": name,
						"partner_code": code,
						"supplier_group": "Production Material",
						"country": "United States" if region == "US" else "Japan",
						"tax_id": f"{region}-T{7_000_000 + i:010d}",
						"risk_rating": rng.choice(["Low", "Low", "Medium"]),
						"certifications": [
							{
								"standard": "IATF 16949",
								"certificate_no": f"IATF-{code}-2025",
								"valid_until": add_days(ctx.end, 400),
							}
						],
						"contacts": contacts,
						"proposed_items": [
							{"item_group": g, "plant": p}
							for g in sorted({g for c in covered for g in COMMODITY_GROUPS[c]})
							for p in plants
						],
					}
				)
				doc.insert()
				req = approvals.request_approval(doc.doctype, doc.name)
			with ctx.as_user(managers[0]):
				approvals.decide(req["name"], "Approve")
			with ctx.as_user(quality[0]):
				approvals.decide(req["name"], "Approve")
			with ctx.as_user(buyer):
				doc.reload()
				doc.submit()
			ctx.count("Supplier")
			for row in frappe.get_all(
				"Approved Supplier List",
				filters={"supplier_onboarding": doc.name},
				pluck="name",
				order_by="name asc",
			):
				asl = frappe.get_doc("Approved Supplier List", row)
				asl.status, asl.ppap_status, asl.share_percent = "Approved", "Approved", 100
				with ctx.as_user(managers[-1]):
					asl.save()
		supplier = frappe.db.get_value("Supplier", {"supplier_name": name}, "name")
		for c in covered:
			by_commodity[c].append(supplier)
	frappe.db.commit()
	return by_commodity


def _commodity_plants(commodity: str) -> list[str]:
	if commodity in ("castings", "forgings", "machining"):
		return ["KYUSHU-PT"]
	if commodity in ("battery", "motors", "electronics", "rubber"):
		return ["AICHI-VA", "KYUSHU-PT"]
	return ["AICHI-VA"]


def carriers(ctx: SeedContext) -> list[str]:
	out = []
	for i in range(int(ctx.profile["partners"]["carriers"])):
		name = names.carrier_name(i)
		if not frappe.db.exists("Supplier", name):
			doc = frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": name,
					"supplier_group": "Carriers",
					"automotive_partner_code": f"C{i + 1:02d}",
				}
			)
			doc.flags.accept_similar_name = True
			doc.insert(ignore_permissions=True)
			ctx.count("Supplier")
		out.append(name)
	return out


def regional(ctx: SeedContext, roster: dict[str, list[str]], profile: str, region: str) -> list[str]:
	"""Group staff (no plant) act for Japan, the North American plant's staff for the US."""
	plant_of = ctx.cache.get("plant_of", {})
	want = (None,) if region == "JP" else ("NA-PDC",)
	return [u for u in roster.get(profile, []) if plant_of.get(u) in want] or roster.get(profile, [])


def dealers(ctx: SeedContext, roster: dict[str, list[str]]) -> dict[str, list[str]]:
	rng = ctx.rng("dealers")
	out = {"JP": [], "US": []}
	controller = roster["Group Controller"][0]
	territory = frappe.db.get_value("Territory", {"is_group": 0}, "name", order_by="name asc")
	for region, count, company, price_list in (
		("JP", ctx.profile["partners"]["dealers_jp"], AICHI, "Dealer Price JP"),
		("US", ctx.profile["partners"]["dealers_us"], NA, "Dealer Price US"),
	):
		liaison = regional(ctx, roster, "Dealer Liaison", region)[0]
		sales_mgr = regional(ctx, roster, "Sales Manager", region)[0]
		for i in range(int(count)):
			name = names.dealer_name(i, region)
			code = f"D{region}{i + 1:02d}"
			if not frappe.db.exists("Customer", {"customer_name": name}):
				first, last = names.person(rng, region)
				with ctx.as_user(liaison):
					doc = frappe.get_doc(
						{
							"doctype": "Dealer Registration",
							"dealer_name": name,
							"dealer_code": code,
							"company": company,
							"territory": territory,
							"country": "Japan" if region == "JP" else "United States",
							"tax_id": f"{region}-D{8_000_000 + i:08d}",
							"requested_credit_limit": 400_000_000 if region == "JP" else 3_000_000,
							"price_list": price_list,
							"authorized_models": [{"vehicle_model": m} for m in ("H1", "H2", "H3")],
							# the dealer principal administers the portal account; larger dealers add a
							# logistics contact who receives the vehicles (portal roles, ERP-10.4)
							"contacts": [
								{
									"first_name": first,
									"last_name": last,
									"email": f"orders@{code.lower()}.dealer.example",
									"partner_role": "Administrator",
								},
								*(
									[
										{
											"first_name": f2,
											"last_name": l2,
											"email": f"yard@{code.lower()}.dealer.example",
											"partner_role": "Logistics",
										}
										for f2, l2 in [names.person(rng, region)]
									]
									if rng.random()
									< float(ctx.profile["partners"]["second_portal_user_share"])
									else []
								),
							],
						}
					)
					doc.insert()
					req = approvals.request_approval(doc.doctype, doc.name)
				with ctx.as_user(sales_mgr):
					approvals.decide(req["name"], "Approve")
				with ctx.as_user(controller):
					approvals.decide(req["name"], "Approve")
				with ctx.as_user(liaison):
					doc.reload()
					doc.submit()
				customer = frappe.db.get_value("Dealer Registration", doc.name, "customer")
				frappe.db.set_value(
					"Customer", customer, "automotive_dealer_tier", rng.choice(["A", "B", "B", "C"])
				)
				ctx.count("Customer")
			out[region].append(frappe.db.get_value("Customer", {"customer_name": name}, "name"))
	frappe.db.commit()
	return out


def bank_accounts(ctx: SeedContext, roster, supplier_names: list[str]) -> None:
	from automotive_core.finance_controls import bank_accounts as controls

	clerk, treasury = roster["AP Clerk"][0], roster["Treasury Approver"][0]
	# group companies settle intercompany invoices through the same verified-account control
	supplier_names = sorted(
		set(supplier_names)
		| set(
			frappe.get_all("Supplier", filters={"is_internal_supplier": 1}, pluck="name", order_by="name asc")
		)
	)
	if not frappe.db.exists("Bank", "Hinode Settlement Bank"):
		frappe.get_doc({"doctype": "Bank", "bank_name": "Hinode Settlement Bank"}).insert(
			ignore_permissions=True
		)
	for i, supplier in enumerate(supplier_names):
		if frappe.db.exists("Bank Account", {"party_type": "Supplier", "party": supplier}):
			continue
		with ctx.as_user(clerk):
			account = frappe.get_doc(
				{
					"doctype": "Bank Account",
					"account_name": f"{supplier[:60]} settlement",
					"bank": "Hinode Settlement Bank",
					"party_type": "Supplier",
					"party": supplier,
					"bank_account_no": f"{9_100_000_000 + i}",
				}
			).insert()
		with ctx.as_user(treasury):
			controls.verify(account.name, "Confirmed by call-back to registered phone number")
		ctx.count("Bank Account")
	frappe.db.commit()


def integration_partners(ctx: SeedContext, suppliers_by_commodity, dealers_by_region) -> None:
	rng = ctx.rng("edi-partners")
	all_suppliers = sorted({s for group in suppliers_by_commodity.values() for s in group})
	for supplier in rng.sample(
		all_suppliers, min(len(all_suppliers), int(ctx.profile["partners"]["edi_suppliers"]))
	):
		_partner(
			supplier,
			"Supplier",
			"asn\npo_acknowledgement",
			"purchase_order.submitted\nsupplier_schedule.published",
		)
	all_dealers = sorted(dealers_by_region["JP"] + dealers_by_region["US"])
	for dealer in rng.sample(all_dealers, min(len(all_dealers), int(ctx.profile["partners"]["edi_dealers"]))):
		_partner(
			dealer,
			"Customer",
			"dealer_order\ndealer_order_change",
			"dealer_order.received\ndealer_order.accepted\nshipment.dispatched",
		)
	frappe.db.commit()


def _partner(party: str, party_type: str, inbound: str, outbound: str) -> None:
	code = frappe.db.get_value(party_type, party, "automotive_partner_code")
	if frappe.db.exists("Integration Partner", f"EDI-{code}"):
		return
	field = "supplier" if party_type == "Supplier" else "customer"
	service = frappe.db.get_value("Partner User Access", {field: party, "status": "Active"}, "user")
	frappe.get_doc(
		{
			"doctype": "Integration Partner",
			"partner_code": f"EDI-{code}",
			"partner_name": party,
			"partner_type": "Supplier" if party_type == "Supplier" else "Dealer",
			"party_type": party_type,
			"party": party,
			"adapter": "Local File Drop",
			"message_format": "Hinode EDI",
			"allowed_message_types": inbound,
			"subscribed_events": outbound,
			"service_user": service,
		}
	).insert(ignore_permissions=True)
