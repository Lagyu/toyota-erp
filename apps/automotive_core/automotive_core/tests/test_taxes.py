"""Tax determination set up by the tenant definition (finance_controls/taxes.py)."""

from __future__ import annotations

import frappe
from erpnext.accounts.doctype.tax_rule.tax_rule import get_tax_template
from frappe.tests import IntegrationTestCase
from frappe.utils import nowdate

from automotive_core.finance_controls.taxes import default_tax_category
from automotive_core.tests.utils import AICHI, KYUSHU, NA


def _template(company: str, tax_type: str, category: str | None) -> str | None:
	return get_tax_template(
		nowdate(), {"company": company, "tax_type": tax_type, "tax_category": category or ""}
	)


class TestTaxDetermination(IntegrationTestCase):
	def test_rules_pick_a_template_per_company_and_category(self):
		self.assertEqual(_template(AICHI, "Sales", "Domestic Taxable"), "Japan Tax 10% - HAMJ")
		self.assertEqual(_template(AICHI, "Sales", "Export Zero-rated"), "Export Zero-rated - HAMJ")
		self.assertEqual(_template(KYUSHU, "Purchase", "Import"), "Import - HPC")
		self.assertEqual(_template(NA, "Sales", "Resale Exempt"), "Resale Exempt - HADNA")
		# parties without a category: domestic tax in Japan; no rule for US purchases
		self.assertEqual(_template(AICHI, "Purchase", None), "Japan Tax 10% - HAMJ")
		self.assertIsNone(_template(NA, "Purchase", None))

	def test_zero_tax_templates_have_no_rows(self):
		for doctype, name in (
			("Sales Taxes and Charges Template", "Export Zero-rated - HAMJ"),
			("Sales Taxes and Charges Template", "Resale Exempt - HADNA"),
			("Purchase Taxes and Charges Template", "Import - HADNA"),
		):
			self.assertEqual(frappe.get_doc(doctype, name).taxes, [], name)
		self.assertEqual(
			frappe.db.get_single_value("Accounts Settings", "add_taxes_from_taxes_and_charges_template"), 1
		)

	def test_default_category_from_company_and_party_country(self):
		self.assertEqual(default_tax_category("Customer", AICHI, "Japan"), "Domestic Taxable")
		self.assertEqual(default_tax_category("Customer", AICHI, "Germany"), "Export Zero-rated")
		self.assertEqual(default_tax_category("Customer", NA, "United States"), "Resale Exempt")
		self.assertEqual(default_tax_category("Supplier", KYUSHU, "United States"), "Import")
		self.assertEqual(default_tax_category("Supplier", NA, "United States"), "Domestic Taxable")
		self.assertIsNone(default_tax_category("Customer", NA, "Japan"))  # no row: Tax Rules decide

	def test_intercompany_parties_carry_their_category(self):
		def category(doctype: str, represents: str) -> str:
			flag = "is_internal_customer" if doctype == "Customer" else "is_internal_supplier"
			return frappe.db.get_value(doctype, {flag: 1, "represents_company": represents}, "tax_category")

		self.assertEqual(category("Customer", NA), "Export Zero-rated")  # Aichi exports to North America
		self.assertEqual(category("Supplier", AICHI), "Import")  # ... which imports them
		self.assertEqual(category("Customer", AICHI), "Domestic Taxable")  # Kyushu supplies Aichi in Japan
		self.assertEqual(category("Supplier", KYUSHU), "Domestic Taxable")
