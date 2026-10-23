"""Fixtures for supply-chain journey tests (test site only; all through ERPNext documents)."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, nowdate

from automotive_core.tests.utils import AICHI, KYUSHU, make_user

AP_CLERK = "ap.sc@hinode.example"
TREASURY1 = "treasury1.sc@hinode.example"
TREASURY2 = "treasury2.sc@hinode.example"
BUYER = "buyer.sc@hinode.example"
PROC_MGR = "procmgr.sc@hinode.example"
PROC_MGR2 = "procmgr2.sc@hinode.example"
QM = "qm.sc@hinode.example"
LIAISON = "liaison.sc@hinode.example"
SALES_MGR = "salesmgr.sc@hinode.example"
CONTROLLER = "controller.sc@hinode.example"
WAREHOUSE = "warehouse.sc@hinode.example"
LOGISTICS = "logistics.sc@hinode.example"
DIRECTOR = "director.sc@hinode.example"


def users() -> None:
	make_user(AP_CLERK, ["AP Clerk", "Accounts User"])
	make_user(TREASURY1, ["Treasury Approver"])
	make_user(TREASURY2, ["Treasury Approver"])
	make_user(BUYER, ["Buyer", "Purchase User"])
	make_user(PROC_MGR, ["Procurement Manager", "Purchase Manager"])
	make_user(PROC_MGR2, ["Procurement Manager", "Purchase Manager"])
	make_user(QM, ["Quality Manager", "Stock User"])
	make_user(LIAISON, ["Dealer Liaison", "Sales User"])
	make_user(SALES_MGR, ["Sales Manager", "Sales User"])
	make_user(CONTROLLER, ["Group Controller", "Accounts Manager"])
	make_user(WAREHOUSE, ["Warehouse Clerk", "Stock User"])
	make_user(LOGISTICS, ["Logistics Coordinator", "Stock User", "Delivery User"])
	make_user(DIRECTOR, ["Plant Director", "Manufacturing Manager", "Stock User"])


def supplier(name: str, group: str = "Production Material", partner_code: str | None = None) -> str:
	if not frappe.db.exists("Supplier", name):
		doc = frappe.get_doc({"doctype": "Supplier", "supplier_name": name, "supplier_group": group,
			"automotive_partner_code": partner_code})
		doc.flags.accept_similar_name = True
		doc.insert(ignore_permissions=True)
	return name


def portal_user(email: str, party_type: str, party: str) -> str:
	make_user(email, [], user_type="Website User")
	if not frappe.db.exists("Partner User Access", {"user": email, "status": "Active"}):
		frappe.get_doc({"doctype": "Partner User Access", "user": email, "party_type": party_type,
			"supplier" if party_type == "Supplier" else "customer": party, "partner_role": "Administrator",
			"from_date": nowdate()}).insert(ignore_permissions=True)
	return email


def blanket_order(supplier_name: str, item_code: str, rate: float, qty: float = 10_000, company: str = AICHI) -> str:
	bo = frappe.get_doc({"doctype": "Blanket Order", "blanket_order_type": "Purchasing", "supplier": supplier_name,
		"company": company, "from_date": add_days(nowdate(), -1), "to_date": add_days(nowdate(), 90),
		"items": [{"item_code": item_code, "qty": qty, "rate": rate}]})
	bo.insert(ignore_permissions=True)
	bo.submit()
	return bo.name


def company_bank_account(company: str = AICHI) -> str:
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"Hinode Settlement Bank - {abbr}"
	if not frappe.db.exists("Account", name):
		parent = frappe.db.get_value("Account", {"company": company, "account_type": "Bank", "is_group": 1}, "name") or \
			frappe.db.get_value("Account", {"company": company, "account_name": "Bank Accounts"}, "name")
		frappe.get_doc({"doctype": "Account", "account_name": "Hinode Settlement Bank", "company": company,
			"parent_account": parent, "account_type": "Bank", "is_group": 0}).insert(ignore_permissions=True)
	if not frappe.db.exists("Mode of Payment", "Wire Transfer"):
		frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": "Wire Transfer", "type": "Bank"}).insert(
			ignore_permissions=True)
	mop = frappe.get_doc("Mode of Payment", "Wire Transfer")
	if not any(a.company == company for a in mop.accounts):
		mop.append("accounts", {"company": company, "default_account": name})
		mop.save(ignore_permissions=True)
	return name


def verified_supplier_bank_account(supplier_name: str) -> str:
	from automotive_core.finance_controls import bank_accounts
	from automotive_core.tests.utils import as_user

	if not frappe.db.exists("Bank", "Hinode Test Bank"):
		frappe.get_doc({"doctype": "Bank", "bank_name": "Hinode Test Bank"}).insert(ignore_permissions=True)
	existing = frappe.db.get_value("Bank Account", {"party_type": "Supplier", "party": supplier_name}, "name")
	if existing:
		return existing
	with as_user(AP_CLERK):
		account = frappe.get_doc({"doctype": "Bank Account", "account_name": f"{supplier_name} main",
			"bank": "Hinode Test Bank", "party_type": "Supplier", "party": supplier_name,
			"bank_account_no": frappe.generate_hash(length=10)}).insert()
	with as_user(TREASURY1):
		bank_accounts.verify(account.name)
	return account.name


def item_price(item_code: str, price_list: str, rate: float) -> None:
	if not frappe.db.exists("Item Price", {"item_code": item_code, "price_list": price_list}):
		frappe.get_doc({"doctype": "Item Price", "item_code": item_code, "price_list": price_list,
			"price_list_rate": rate}).insert(ignore_permissions=True)


def transfer_price(seller: str, buyer: str, item_code: str, price: float, currency: str = "JPY") -> str:
	from automotive_core.tests.utils import approve

	tpa = frappe.get_doc({"doctype": "Transfer Price Agreement", "seller_company": seller, "buyer_company": buyer,
		"item_code": item_code, "pricing_method": "Fixed Price", "price": price, "currency": currency,
		"valid_from": add_days(nowdate(), -1), "basis": "Cost plus 8% benchmark (test)"})
	tpa.insert(ignore_permissions=True)
	approve(tpa, "Administrator", [CONTROLLER])
	tpa.reload()
	tpa.submit()
	return tpa.name


KYUSHU_FG = "Kyushu Finished Powertrains - HPC"
KYUSHU = KYUSHU
