"""Taxes on dealer, supplier and purchasing documents (ENT-7, ERP-08.4, ERP-09.2): the party's
tax category picks the template, and documents created by code carry its rows."""

from __future__ import annotations

import frappe
from erpnext.accounts.party import get_party_details
from frappe.utils import add_days, flt, nowdate

from automotive_core.tests.utils import AICHI, NA
from automotive_manufacturing.tests import catalog
from automotive_supply_chain.tests import helpers as h
from automotive_supply_chain.tests.test_journeys import JourneyTestCase


def _registration(company: str, country: str, code: str) -> frappe._dict:
	return frappe.get_doc(
		{
			"doctype": "Dealer Registration",
			"dealer_name": f"Tax Test Dealer {code}",
			"dealer_code": code,
			"company": company,
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			"country": country,
			"tax_id": f"TAX-{code}",
			"requested_credit_limit": 1_000_000,
			"price_list": "Dealer Price JP" if company == AICHI else "Dealer Price US",
			"contacts": [],
		}
	).insert(ignore_permissions=True)


def _onboarding(country: str, code: str) -> frappe._dict:
	return frappe.get_doc(
		{
			"doctype": "Supplier Onboarding",
			"supplier_name": f"Tax Test Supplier {code}",
			"partner_code": code,
			"supplier_group": "Production Material",
			"country": country,
			"tax_id": f"TAX-{code}",
			"certifications": [
				{
					"standard": "IATF 16949",
					"certificate_no": f"IATF-{code}",
					"valid_until": add_days(nowdate(), 365),
				}
			],
			"contacts": [],
			"proposed_items": [{"item_group": "Raw Material", "plant": catalog.PLANT}],
		}
	).insert(ignore_permissions=True)


class TestTaxes(JourneyTestCase):
	def test_registrations_default_the_tax_category(self):
		self.assertEqual(_registration(AICHI, "Japan", "TAXJP").tax_category, "Domestic Taxable")
		self.assertEqual(_registration(NA, "United States", "TAXUS").tax_category, "Resale Exempt")
		self.assertEqual(_onboarding("Japan", "TAXS1").tax_category, "Domestic Taxable")
		self.assertEqual(_onboarding("United States", "TAXS2").tax_category, "Import")  # supplies Aichi
		chosen = _registration(AICHI, "Japan", "TAXEX")
		chosen.tax_category = "Export Zero-rated"
		chosen.save(ignore_permissions=True)
		self.assertEqual(chosen.tax_category, "Export Zero-rated")  # an explicit choice is kept

	def test_party_category_picks_the_template(self):
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "Tax Test US Dealer",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
				"tax_category": "Resale Exempt",
			}
		).insert(ignore_permissions=True)

		def template(company: str) -> str | None:  # what a new sales order for the customer gets
			return get_party_details(
				party=customer.name,
				party_type="Customer",
				company=company,
				doctype="Sales Order",
				posting_date=nowdate(),
			).get("taxes_and_charges")

		self.assertEqual(template(NA), "Resale Exempt - HADNA")
		customer.db_set("tax_category", "Domestic Taxable")
		self.assertEqual(template(AICHI), "Japan Tax 10% - HAMJ")

	def test_purchase_orders_charge_consumption_tax_except_on_imports(self):
		domestic = h.supplier("Tax Test Domestic Supplier")
		imported = h.supplier("Tax Test Import Supplier")
		frappe.db.set_value("Supplier", domestic, "tax_category", "Domestic Taxable")
		frappe.db.set_value("Supplier", imported, "tax_category", "Import")

		def order(supplier: str):
			return frappe.get_doc(
				{
					"doctype": "Purchase Order",
					"supplier": supplier,
					"company": AICHI,
					"transaction_date": nowdate(),
					"schedule_date": add_days(nowdate(), 3),
					"items": [
						{
							"item_code": self.cat["wheel"],
							"qty": 10,
							"rate": 1_200,
							"warehouse": catalog.PARTS_STORE,
							"schedule_date": add_days(nowdate(), 3),
						}
					],
				}
			).insert(ignore_permissions=True)

		po = order(domestic)
		self.assertEqual(po.taxes_and_charges, "Japan Tax 10% - HAMJ")
		self.assertEqual([(t.account_head, flt(t.rate)) for t in po.taxes], [("CT 10% - HAMJ", 10)])
		self.assertEqual(flt(po.total_taxes_and_charges), 1_200)
		self.assertEqual(flt(po.grand_total), 13_200)
		po = order(imported)
		self.assertEqual(
			(po.taxes_and_charges, len(po.taxes), flt(po.grand_total)), ("Import - HAMJ", 0, 12_000)
		)
