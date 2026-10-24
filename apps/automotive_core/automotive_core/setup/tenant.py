"""Apply a tenant definition (setup/tenants/<tenant>.yaml): companies, fiscal years,
calendars, departments, branches, warehouses, plants, shifts, workstations, lines,
tax categories, templates and rules, intercompany parties, segregation-of-duties rules and
approval policies.

Idempotent: every step looks up what exists and only creates or corrects what is
missing, so it doubles as the drift check for a tenant (``apply(..., dry_run=True)``
reports what would change). Uses ERPNext's own setup wizard for the first company.

    bench --site SITE execute automotive_core.setup.tenant.apply --kwargs "{'tenant': 'hinode'}"
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import frappe
import holidays as holiday_calendars
import yaml
from frappe.utils import getdate

from automotive_core.finance_controls.taxes import default_tax_category
from automotive_core.utils import hook_list

TENANT_DIR = Path(__file__).parent / "tenants"
WEEKDAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
ROLE_FIELDS = {
	"receiving": ("receiving_warehouse", "Receiving"),
	"inspection": ("inspection_warehouse", "Receiving Inspection"),
	"raw_material": ("raw_material_warehouse", "Raw Material"),
	"wip": ("wip_warehouse", "Work In Progress"),
	"quarantine": ("quarantine_warehouse", "Quarantine"),
	"finished_goods": ("finished_goods_warehouse", "Finished Goods"),
	"outbound": ("outbound_warehouse", "Outbound"),
	"in_transit": ("in_transit_warehouse", "In Transit"),
	"scrap": ("scrap_warehouse", "Scrap"),
	"consignment": ("consignment_warehouse", "Consignment"),
}


class TenantLog:
	def __init__(self, dry_run: bool):
		self.dry_run = dry_run
		self.changes: list[str] = []

	def change(self, message: str) -> bool:
		self.changes.append(message)
		return not self.dry_run


def load(tenant: str) -> dict[str, Any]:
	path = TENANT_DIR / f"{tenant}.yaml"
	if not path.exists():
		frappe.throw(f"Unknown tenant definition {tenant!r} ({path})")
	return yaml.safe_load(path.read_text())


def apply(tenant: str = "hinode", dry_run: bool = False, environment: str | None = None) -> list[str]:
	"""environment: development | test | staging | production (default from site_config
	``automotive_environment``). Production additionally enforces 2FA for privileged roles."""
	frappe.only_for("System Manager")
	definition = load(tenant)
	log = TenantLog(dry_run)
	environment = environment or frappe.conf.get("automotive_environment") or "development"
	companies = definition["companies"]
	_setup_wizard(definition, companies[0], log)
	_singles(definition.get("settings", {}), log)
	if environment == "production":
		_singles(definition.get("production_settings", {}), log)
	for company in companies:
		_company(company, log)
	_fiscal_years(definition, log)
	_holiday_lists(definition, log)
	for company in companies:
		_company_defaults(company, definition, log)
	_branches(companies, log)
	_departments(definition, companies, log)
	_shift_patterns(definition, log)
	for plant in definition["plants"]:
		_plant(plant, companies, definition, log)
	_taxes(definition, companies, log)
	_intercompany(definition, companies, log)
	_sod_rules(definition, log)
	_approval_policies(definition, log)
	for path in hook_list("automotive_tenant_extensions"):
		frappe.get_attr(path)(definition, log)
	if not dry_run:
		frappe.db.commit()
	return log.changes


# -------------------------------------------------------------------------------------
# Companies, years, calendars
# -------------------------------------------------------------------------------------


def _setup_wizard(definition, first: dict, log: TenantLog) -> None:
	if frappe.is_setup_complete():
		return
	if not log.change(f"setup wizard: {first['name']}"):
		return
	from frappe.desk.page.setup_wizard.setup_wizard import setup_complete

	year = definition["fiscal_years"][0]
	result = setup_complete(
		{
			"language": "English",
			"country": first["country"],
			"timezone": "Asia/Tokyo",
			"currency": first["currency"],
			"company_name": first["name"],
			"company_abbr": first["abbr"],
			"chart_of_accounts": "Standard",
			"fy_start_date": year["start"],
			"fy_end_date": year["end"],
			"setup_demo": 0,
		}
	)
	if (result or {}).get("status") != "ok":
		frappe.throw(f"ERPNext setup wizard failed: {result}")
	frappe.clear_cache()


def _same(current, wanted) -> bool:
	if isinstance(wanted, bool | int | float):
		return frappe.utils.flt(current) == frappe.utils.flt(wanted)
	return str(current if current is not None else "") == str(wanted)


def _singles(settings: dict, log: TenantLog) -> None:
	for doctype, values in settings.items():
		doc = frappe.get_single(doctype)
		changes = {k: v for k, v in values.items() if not _same(doc.get(k), v)}
		if not changes or not log.change(f"{doctype}: {sorted(changes)}"):
			continue
		doc.update(changes)
		doc.flags.ignore_permissions = True
		doc.save()


def _company(company: dict, log: TenantLog) -> None:
	if frappe.db.exists("Company", company["name"]):
		return
	if not log.change(f"company {company['name']}"):
		return
	chart = "Standard"
	frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": company["name"],
			"abbr": company["abbr"],
			"default_currency": company["currency"],
			"country": company["country"],
			"create_chart_of_accounts_based_on": "Standard Template",
			"chart_of_accounts": chart,
			"tax_id": company.get("tax_id"),
			"enable_perpetual_inventory": 1,
		}
	).insert(ignore_permissions=True)


def _fiscal_years(definition, log: TenantLog) -> None:
	for year in definition["fiscal_years"]:
		start, end = getdate(year["start"]), getdate(year["end"])
		existing = frappe.db.get_value("Fiscal Year", {"year_start_date": start, "year_end_date": end}, "name")
		if existing:
			continue
		if not log.change(f"fiscal year {year['year']}"):
			continue
		frappe.get_doc(
			{"doctype": "Fiscal Year", "year": year["year"], "year_start_date": start, "year_end_date": end}
		).insert(ignore_permissions=True)


def holiday_list_name(region: str, year: int) -> str:
	return f"{region} {year} Plant Calendar"


def _holiday_lists(definition, log: TenantLog) -> None:
	regions = {c["holidays"] for c in definition["companies"]}
	for region in sorted(regions):
		for year in definition["holiday_years"]:
			name = holiday_list_name(region, year)
			if frappe.db.exists("Holiday List", name):
				continue
			if not log.change(f"holiday list {name}"):
				continue
			calendar = holiday_calendars.country_holidays(region, years=year)
			days: dict[dt.date, str] = {d: str(n) for d, n in calendar.items()}
			if region == "JP":  # plant shutdowns (New Year, Golden Week, Obon) on top of public holidays
				for month, day_from, day_to, label in ((1, 1, 3, "New Year shutdown"), (5, 1, 5, "Golden Week shutdown"),
						(8, 13, 16, "Obon shutdown"), (12, 29, 31, "Year-end shutdown")):
					for day in range(day_from, day_to + 1):
						days.setdefault(dt.date(year, month, day), label)
			doc = frappe.get_doc(
				{
					"doctype": "Holiday List",
					"holiday_list_name": name,
					"from_date": dt.date(year, 1, 1),
					"to_date": dt.date(year, 12, 31),
					"holidays": [{"holiday_date": d, "description": label} for d, label in sorted(days.items())],
				}
			)
			for weekday in ("Saturday", "Sunday"):
				doc.weekly_off = weekday
				doc.get_weekly_off_dates()
			doc.insert(ignore_permissions=True)


def _company_defaults(company: dict, definition, log: TenantLog) -> None:
	doc = frappe.get_doc("Company", company["name"])
	this_year = frappe.utils.getdate().year
	years = definition["holiday_years"]
	year = this_year if this_year in years else years[0]
	wanted = holiday_list_name(company["holidays"], year)
	if doc.default_holiday_list != wanted and frappe.db.exists("Holiday List", wanted):
		if log.change(f"{company['abbr']}: default holiday list {wanted}"):
			frappe.db.set_value("Company", doc.name, "default_holiday_list", wanted)


def _branches(companies, log: TenantLog) -> None:
	for company in companies:
		branch = company.get("branch")
		if branch and not frappe.db.exists("Branch", branch) and log.change(f"branch {branch}"):
			frappe.get_doc({"doctype": "Branch", "branch": branch}).insert(ignore_permissions=True)


def _departments(definition, companies, log: TenantLog) -> None:
	for company in companies:
		root = frappe.db.get_value("Department", {"department_name": "All Departments"}, "name") or "All Departments"
		for department in definition["departments"]:
			if frappe.db.exists("Department", {"department_name": department, "company": company["name"]}):
				continue
			if not log.change(f"department {department} - {company['abbr']}"):
				continue
			frappe.get_doc(
				{"doctype": "Department", "department_name": department, "company": company["name"],
				 "parent_department": root}
			).insert(ignore_permissions=True)


def _shift_patterns(definition, log: TenantLog) -> None:
	this_year = frappe.utils.getdate().year
	years = definition["holiday_years"]
	year = this_year if this_year in years else years[0]
	for pattern in definition["shift_patterns"]:
		if frappe.db.exists("Shift Pattern", pattern["name"]):
			continue
		if not log.change(f"shift pattern {pattern['name']}"):
			continue
		doc = frappe.get_doc(
			{
				"doctype": "Shift Pattern",
				"pattern_name": pattern["name"],
				"time_zone": pattern["time_zone"],
				"holiday_list": holiday_list_name(pattern["holidays"], year),
				**{day.lower(): int(day.lower() in pattern["days"]) for day in WEEKDAY_NAMES},
				"shifts": [
					{"shift_code": s["code"], "start_time": s["start"], "end_time": s["end"],
					 "break_minutes": s.get("break_minutes", 0)}
					for s in pattern["shifts"]
				],
			}
		)
		doc.insert(ignore_permissions=True)


# -------------------------------------------------------------------------------------
# Plants, warehouses, workstations, lines
# -------------------------------------------------------------------------------------


def _abbr(companies, company_name: str) -> str:
	return next(c["abbr"] for c in companies if c["name"] == company_name)


def _warehouse(name: str, company: str, abbr: str, parent: str, log: TenantLog, *, is_group=False,
		plant: str | None = None, role: str | None = None, transit: bool = False) -> str:
	full = f"{name} - {abbr}"
	if frappe.db.exists("Warehouse", full):
		updates = {}
		if plant and frappe.db.get_value("Warehouse", full, "automotive_plant") != plant:
			updates["automotive_plant"] = plant
		if role and frappe.db.get_value("Warehouse", full, "automotive_warehouse_role") != role:
			updates["automotive_warehouse_role"] = role
		if updates and log.change(f"warehouse {full}: {updates}"):
			frappe.db.set_value("Warehouse", full, updates)
		return full
	if not log.change(f"warehouse {full}"):
		return full
	doc = frappe.get_doc(
		{
			"doctype": "Warehouse",
			"warehouse_name": name,
			"company": company,
			"parent_warehouse": parent,
			"is_group": int(is_group),
			"automotive_plant": plant,
			"automotive_warehouse_role": role,
			"warehouse_type": "Transit" if transit else None,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_transit_type() -> None:
	if not frappe.db.exists("Warehouse Type", "Transit"):
		frappe.get_doc({"doctype": "Warehouse Type", "name": "Transit"}).insert(ignore_permissions=True)


def _cost_center(plant: dict, abbr: str, log: TenantLog) -> str:
	name = f"{plant['name']} - {abbr}"
	if frappe.db.exists("Cost Center", name):
		return name
	if not log.change(f"cost center {name}"):
		return name
	parent = frappe.db.get_value(
		"Cost Center", {"company": plant["company"], "is_group": 1, "parent_cost_center": ("is", "not set")}, "name"
	)
	doc = frappe.get_doc(
		{"doctype": "Cost Center", "cost_center_name": plant["name"], "company": plant["company"],
		 "parent_cost_center": parent, "is_group": 0}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _plant(plant: dict, companies, definition, log: TenantLog) -> None:
	_ensure_transit_type()
	company, abbr, code = plant["company"], _abbr(companies, plant["company"]), plant["code"]
	root = frappe.db.get_value(
		"Warehouse", {"company": company, "is_group": 1, "parent_warehouse": ("is", "not set")}, "name"
	)
	if not root and not log.dry_run:
		frappe.throw(f"Company {company} has no root warehouse")
	exists = frappe.db.exists("Plant", code)
	if not exists and log.change(f"plant {code}"):
		frappe.get_doc(
			{"doctype": "Plant", "plant_code": code, "plant_name": plant["name"], "company": company,
			 "plant_type": plant["type"], "time_zone": plant["time_zone"], "country": plant["country"]}
		).insert(ignore_permissions=True)
	site_group = _warehouse(plant["name"], company, abbr, root, log, is_group=True, plant=code)
	values: dict[str, Any] = {}
	for key, (field, role) in ROLE_FIELDS.items():
		name = plant["warehouses"].get(key)
		if name:
			values[field] = _warehouse(name, company, abbr, site_group, log, plant=code, role=role,
				transit=key == "in_transit")
	line_side_group = _warehouse(f"{plant['name']} Line Side", company, abbr, site_group, log, is_group=True,
		plant=code, role="Line Side")
	values["line_side_warehouse"] = line_side_group
	values["cost_center"] = _cost_center(plant, abbr, log)
	values["branch"] = next((c.get("branch") for c in companies if c["name"] == company), None)
	this_year = frappe.utils.getdate().year
	years = definition["holiday_years"]
	region = next(c["holidays"] for c in companies if c["name"] == company)
	values["holiday_list"] = holiday_list_name(region, this_year if this_year in years else years[0])
	values["default_shift_pattern"] = plant["shift_pattern"]
	values["annual_capacity_units"] = plant.get("annual_capacity_units")
	if log.dry_run:
		return
	doc = frappe.get_doc("Plant", code)
	if any(doc.get(k) != v for k, v in values.items()):
		log.change(f"plant {code}: defaults")
		doc.update(values)
		doc.save(ignore_permissions=True)
	for line in plant["lines"]:
		_line(line, plant, company, abbr, line_side_group, log)


def _workstation(name: str, plant: dict, log: TenantLog) -> str:
	if frappe.db.exists("Workstation", name):
		return name
	if not log.change(f"workstation {name}"):
		return name
	pattern = frappe.get_doc("Shift Pattern", plant["shift_pattern"])
	doc = frappe.get_doc(
		{
			"doctype": "Workstation",
			"workstation_name": name,
			"production_capacity": 1,
			"holiday_list": pattern.holiday_list,
			"automotive_plant": plant["code"],
			"working_hours": _working_hours(pattern),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _hms(value) -> str:
	"""Time fields come back from MariaDB as timedelta; ERPNext expects HH:MM:SS strings."""
	if isinstance(value, dt.timedelta):
		seconds = int(value.total_seconds())
		return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
	return str(value)


def _working_hours(pattern) -> list[dict]:
	"""Workstation working hours; a shift crossing midnight becomes two rows."""
	rows = []
	for slot in pattern.shifts:
		start, end = _hms(slot.start_time), _hms(slot.end_time)
		if slot.crosses_midnight:
			rows.append({"start_time": start, "end_time": "23:59:59", "enabled": 1})
			rows.append({"start_time": "00:00:00", "end_time": end, "enabled": 1})
		else:
			rows.append({"start_time": start, "end_time": end, "enabled": 1})
	return rows


def _line(line: dict, plant: dict, company: str, abbr: str, line_side_group: str, log: TenantLog) -> None:
	code = line["code"]
	line_side = _warehouse(f"{line['name']} Line Side", company, abbr, line_side_group, log, plant=plant["code"],
		role="Line Side")
	stations = []
	for seq, station in enumerate(line["stations"], start=1):
		station_code, _sep, _label = station.partition(" ")
		workstation = _workstation(f"{code}-{station_code}", plant, log)
		stations.append(
			{
				"station_code": station_code,
				"sequence": seq * 10,
				"workstation": workstation,
				"station_type": "Inspection" if ("Inspection" in station or "Test" in station or "Audit" in station
					or "PDI" in station) else "Assembly",
				"is_quality_gate": int("Inspection" in station or "Test" in station or "PDI" in station),
				"cycle_time_seconds": line["takt"],
			}
		)
	if frappe.db.exists("Production Line", code):
		return
	if not log.change(f"production line {code}"):
		return
	frappe.get_doc(
		{
			"doctype": "Production Line",
			"line_code": code,
			"line_name": line["name"],
			"plant": plant["code"],
			"line_type": line["type"],
			"shift_pattern": plant["shift_pattern"],
			"takt_time_seconds": line["takt"],
			"line_side_warehouse": line_side,
			"default_workstation": stations[-1]["workstation"],
			"stations": stations,
		}
	).insert(ignore_permissions=True)


# -------------------------------------------------------------------------------------
# Parties, controls
# -------------------------------------------------------------------------------------


# -------------------------------------------------------------------------------------
# Taxes (finance_controls/taxes.py)
# -------------------------------------------------------------------------------------


def _taxes(definition, companies, log: TenantLog) -> None:
	taxes = definition.get("taxes") or {}
	company_of = {c["abbr"]: c["name"] for c in companies}
	for name in taxes.get("categories", []):
		if not frappe.db.exists("Tax Category", name) and log.change(f"tax category {name}"):
			frappe.get_doc({"doctype": "Tax Category", "title": name}).insert(ignore_permissions=True)
	for template in taxes.get("templates", []):
		# templates without rows: the document records why it carries no tax
		doctype = f"{template['type']} Taxes and Charges Template"
		for abbr in template["companies"]:
			name = f"{template['title']} - {abbr}"
			if not frappe.db.exists(doctype, name) and log.change(f"{doctype} {name}"):
				frappe.get_doc({"doctype": doctype, "title": template["title"], "company": company_of[abbr]}).insert(
					ignore_permissions=True)
	for abbr, by_type in (taxes.get("rules") or {}).items():
		for tax_type, by_category in by_type.items():
			for category, title in by_category.items():
				_tax_rule(company_of[abbr], tax_type, category or None, f"{title} - {abbr}", log)
	wanted = [(r["party_type"], r.get("company_country"), r.get("party_country"), r["tax_category"])
		for r in taxes.get("party_defaults", [])]
	settings = frappe.get_single("Automotive Settings")
	current = [(r.party_type, r.company_country or None, r.party_country or None, r.tax_category)
		for r in settings.get("tax_category_defaults")]
	if current != wanted and log.change(f"Automotive Settings: {len(wanted)} tax category defaults"):
		settings.set("tax_category_defaults", [dict(zip(("party_type", "company_country", "party_country",
			"tax_category"), row, strict=True)) for row in wanted])
		settings.save(ignore_permissions=True)


# a rule the tenant manages: company, tax type and category only (rules with other filters are the
# operator's own)
_RULE_FILTERS_NOT_SET = ("customer", "supplier", "customer_group", "supplier_group", "item", "item_group",
	"billing_country", "shipping_country", "from_date", "to_date")


def _tax_rule(company: str, tax_type: str, category: str | None, template: str, log: TenantLog) -> None:
	field = "sales_tax_template" if tax_type == "Sales" else "purchase_tax_template"
	filters = {"company": company, "tax_type": tax_type, "tax_category": category or ("is", "not set"),
		**{f: ("is", "not set") for f in _RULE_FILTERS_NOT_SET}}
	existing = frappe.get_all("Tax Rule", filters=filters, fields=["name", field], limit=1)
	label = f"tax rule {company} {tax_type} {category or '(no category)'} -> {template}"
	if existing and existing[0][field] == template:
		return
	if not log.change(label):
		return
	if existing:
		frappe.db.set_value("Tax Rule", existing[0].name, field, template)
		return
	# no shopping cart here (the rule's default would also apply it there)
	frappe.get_doc({"doctype": "Tax Rule", "tax_type": tax_type, "company": company, "tax_category": category,
		"use_for_shopping_cart": 0, field: template}).insert(ignore_permissions=True)


def _intercompany(definition, companies, log: TenantLog) -> None:
	currency = {c["name"]: c["currency"] for c in companies}
	for pair in definition.get("intercompany", []):
		seller, buyer = pair["seller"], pair["buyer"]
		# ERPNext requires intercompany documents to use a buying+selling price list; the
		# pair trades in the buyer's currency.
		price_list = _intercompany_price_list(currency[buyer], log)
		# a seller billing in the buyer's (foreign) currency needs a receivable in that currency
		receivable = _party_account(seller, currency[buyer], "Receivable", log) \
			if currency[seller] != currency[buyer] else None
		_internal_party("Customer", buyer, seller, log, price_list, receivable, currency[buyer])
		_internal_party("Supplier", seller, buyer, log, price_list)


def _party_account(company: str, currency: str, account_type: str, log: TenantLog) -> str | None:
	label = "Debtors" if account_type == "Receivable" else "Creditors"
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"{label} {currency} - {abbr}"
	if frappe.db.exists("Account", name):
		return name
	parent = frappe.db.get_value("Account", {"company": company, "account_type": account_type, "is_group": 0,
		"account_currency": frappe.get_cached_value("Company", company, "default_currency")}, "parent_account")
	if not log.change(f"account {name}"):
		return name
	frappe.get_doc({"doctype": "Account", "account_name": f"{label} {currency}", "company": company,
		"parent_account": parent, "account_type": account_type, "account_currency": currency,
		"is_group": 0}).insert(ignore_permissions=True)
	return name


def _intercompany_price_list(currency: str, log: TenantLog) -> str:
	name = f"Intercompany {currency}"
	if not frappe.db.exists("Price List", name) and log.change(f"price list {name}"):
		frappe.get_doc({"doctype": "Price List", "price_list_name": name, "currency": currency, "buying": 1,
			"selling": 1, "enabled": 1}).insert(ignore_permissions=True)
	return name


def _internal_party(doctype: str, represents: str, transacting_company: str, log: TenantLog,
		price_list: str | None = None, party_account: str | None = None, billing_currency: str | None = None) -> None:
	name_field = "customer_name" if doctype == "Customer" else "supplier_name"
	flag = "is_internal_customer" if doctype == "Customer" else "is_internal_supplier"
	existing = frappe.db.get_value(doctype, {flag: 1, "represents_company": represents}, "name")
	tax_category = default_tax_category(doctype, transacting_company,
		frappe.get_cached_value("Company", represents, "country"))
	if existing:
		doc = frappe.get_doc(doctype, existing)
		drift = {}
		if price_list and doc.get("default_price_list") != price_list:
			drift["default_price_list"] = price_list
		if doctype == "Supplier":
			drift.update({f: 1 for f in ("allow_purchase_invoice_creation_without_purchase_order",
				"allow_purchase_invoice_creation_without_purchase_receipt") if not doc.get(f)})
			if drift and log.change(f"Supplier {existing}: {sorted(drift)}"):
				doc.update(drift)
				doc.flags.accept_similar_name = True
				doc.save(ignore_permissions=True)
		changed = False
		if billing_currency and doc.get("default_currency") != billing_currency and log.change(
				f"{doctype} {existing}: billing currency {billing_currency}"):
			doc.default_currency = billing_currency
			changed = True
		if party_account and not any(r.company == transacting_company and r.account == party_account
				for r in doc.accounts) and log.change(f"{doctype} {existing}: account {party_account}"):
			doc.set("accounts", [r for r in doc.accounts if r.company != transacting_company])
			doc.append("accounts", {"company": transacting_company, "account": party_account})
			changed = True
		if not any(row.company == transacting_company for row in doc.companies) and log.change(
				f"{doctype} {existing}: allowed company {transacting_company}"):
			doc.append("companies", {"company": transacting_company})
			changed = True
		if tax_category and doc.get("tax_category") != tax_category and log.change(
				f"{doctype} {existing}: tax category {tax_category}"):
			doc.tax_category = tax_category
			changed = True
		if changed:
			doc.flags.accept_similar_name = True
			doc.save(ignore_permissions=True)
		return
	if not log.change(f"internal {doctype} {represents}"):
		return
	values = {
		"doctype": doctype,
		name_field: represents,
		flag: 1,
		"represents_company": represents,
		"companies": [{"company": transacting_company}],
		"default_price_list": price_list,
		"accounts": [{"company": transacting_company, "account": party_account}] if party_account else [],
		"tax_category": tax_category,
	}
	if billing_currency:
		values["default_currency"] = billing_currency
	if doctype == "Customer":
		values["customer_group"] = frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
		values["territory"] = frappe.db.get_value("Territory", {"is_group": 0}, "name")
	else:
		values["supplier_group"] = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		# Intercompany invoices mirror the seller's invoice (matched in three_way_match), not a PO/PR pair.
		values["allow_purchase_invoice_creation_without_purchase_order"] = 1
		values["allow_purchase_invoice_creation_without_purchase_receipt"] = 1
	doc = frappe.get_doc(values)
	doc.flags.accept_similar_name = True  # internal parties mirror company names on purpose
	doc.insert(ignore_permissions=True)


def _sod_rules(definition, log: TenantLog) -> None:
	for rule in definition.get("segregation_of_duties", []):
		if frappe.db.exists("Segregation of Duties Rule", rule["name"]):
			continue
		if not log.change(f"SoD rule {rule['name']}"):
			continue
		frappe.get_doc(
			{"doctype": "Segregation of Duties Rule", "rule_name": rule["name"], "role_a": rule["a"], "role_b": rule["b"],
			 "enforcement": rule["enforcement"], "rationale": rule["rationale"], "is_active": 1}
		).insert(ignore_permissions=True)


def approval_policy(policy: dict, log: TenantLog) -> None:
	if not frappe.db.exists("DocType", policy["doctype"]):
		return
	companies = policy.get("companies") or [None]
	for company in companies:
		name = policy["name"] if len(companies) == 1 else f"{policy['name']} ({company})"
		if frappe.db.exists("Approval Policy", name):
			continue
		if not log.change(f"approval policy {name}"):
			continue
		frappe.get_doc(
			{
				"doctype": "Approval Policy",
				"policy_name": name,
				"reference_doctype": policy["doctype"],
				"applies_to_company": company,
				"is_active": 1,
				"priority": policy.get("priority", 10),
				"amount_field": policy.get("amount_field"),
				"min_amount": policy.get("min_amount", 0),
				"max_amount": policy.get("max_amount", 0),
				"category_field": policy.get("category_field"),
				"category_values": "\n".join(policy.get("category_values", [])),
				"maker_cannot_approve": 1,
				"distinct_approvers": 1,
				"allow_delegation": 1,
				"bind_to_document_hash": 1,
				"steps": [
					{"step_no": s["step"], "step_name": s["name"], "approver_role": s["role"],
					 "min_amount": s.get("min_amount", 0)}
					for s in policy["steps"]
				],
			}
		).insert(ignore_permissions=True)


def _approval_policies(definition, log: TenantLog) -> None:
	for policy in definition.get("approval_policies", []):
		approval_policy(policy, log)
