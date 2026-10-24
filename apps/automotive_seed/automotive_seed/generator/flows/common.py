"""Lookups shared by the simulation flows."""

from __future__ import annotations

import frappe

COMPANY_ABBR = {
	"Hinode Automotive Manufacturing Japan": "HAMJ",
	"Hinode Powertrain Components": "HPC",
	"Hinode Automotive Distribution North America": "HADNA",
}


def plant(name: str):
	return frappe.get_cached_doc("Plant", name)


def store_for(plant_name: str) -> str:
	return plant(plant_name).raw_material_warehouse


def reserve_for(plant_name: str) -> str | None:
	return plant(plant_name).reserve_warehouse


def fg_for(plant_name: str) -> str:
	return plant(plant_name).finished_goods_warehouse


def line_side_for(line: str) -> str:
	return frappe.get_cached_value("Production Line", line, "line_side_warehouse")


def company_bank_account(company: str) -> str:
	abbr = COMPANY_ABBR[company]
	name = f"Hinode Settlement Bank - {abbr}"
	if not frappe.db.exists("Account", name):
		parent = frappe.db.get_value(
			"Account", {"company": company, "account_type": "Bank", "is_group": 1}, "name"
		) or frappe.db.get_value(
			"Account", {"company": company, "account_name": "Bank Accounts", "is_group": 1}, "name"
		)
		currency = frappe.get_cached_value("Company", company, "default_currency")
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "Hinode Settlement Bank",
				"company": company,
				"parent_account": parent,
				"account_type": "Bank",
				"is_group": 0,
				"account_currency": currency,
			}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Mode of Payment", "Wire Transfer"):
		frappe.get_doc(
			{"doctype": "Mode of Payment", "mode_of_payment": "Wire Transfer", "type": "Bank"}
		).insert(ignore_permissions=True)
	mop = frappe.get_doc("Mode of Payment", "Wire Transfer")
	if not any(a.company == company for a in mop.accounts):
		mop.append("accounts", {"company": company, "default_account": name})
		mop.save(ignore_permissions=True)
	return name
