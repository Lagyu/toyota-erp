"""Enterprise structure, plant scoping, effective-dated access, master data and finance controls."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from automotive_core.access.master_data import DuplicateMasterDataError
from automotive_core.access.plant_scope import derive_plant
from automotive_core.finance_controls import bank_accounts
from automotive_core.setup import tenant
from automotive_core.tests.utils import AICHI, KYUSHU, NA, as_user, make_user


class TestTenantStructure(IntegrationTestCase):
	def test_reference_structure_and_idempotence(self):
		self.assertEqual(tenant.apply("hinode", dry_run=True), [])
		self.assertEqual(frappe.db.count("Company", {"name": ("in", (AICHI, KYUSHU, NA))}), 3)
		self.assertEqual(frappe.db.count("Plant"), 3)
		plant_warehouses = frappe.db.count("Warehouse", {"is_group": 0, "automotive_plant": ("is", "set")})
		self.assertGreaterEqual(plant_warehouses, 15)
		roles = set(frappe.get_all("Warehouse", filters={"automotive_plant": "AICHI-VA"},
			pluck="automotive_warehouse_role"))
		for role in ("Receiving", "Quarantine", "Line Side", "Work In Progress", "Finished Goods", "In Transit",
				"Consignment", "Outbound"):
			self.assertIn(role, roles)
		self.assertEqual(frappe.db.get_value("Company", NA, "default_currency"), "USD")
		self.assertTrue(frappe.db.exists("Customer", {"is_internal_customer": 1, "represents_company": AICHI}))
		line = frappe.get_doc("Production Line", "AICHI-FA1")
		self.assertEqual(line.max_units_per_day, (960 * 60 // 64))
		self.assertEqual(len(line.stations), 5)

	def test_holiday_calendars_include_plant_shutdowns(self):
		holidays = frappe.get_all("Holiday", filters={"parent": "JP 2026 Plant Calendar"}, pluck="holiday_date")
		self.assertIn(frappe.utils.getdate("2026-05-04"), holidays)  # Golden Week
		self.assertIn(frappe.utils.getdate("2026-08-14"), holidays)  # Obon shutdown


class TestPlantScope(IntegrationTestCase):
	def test_plant_is_derived_from_warehouse_and_checked_against_company(self):
		doc = frappe.new_doc("Material Request")
		doc.company = AICHI
		doc.material_request_type = "Material Transfer"
		doc.set_warehouse = "Aichi Parts Store - HAMJ"
		self.assertEqual(derive_plant(doc), "AICHI-VA")
		doc.automotive_plant = "KYUSHU-PT"
		with self.assertRaises(frappe.ValidationError):
			from automotive_core.access.plant_scope import ensure_plant

			ensure_plant(doc)

	def test_plant_user_permission_filters_lists(self):
		from automotive_core.access.user_permissions import sync

		user = make_user("aichi.planner@hinode.example", ["Production Planner", "Manufacturing User", "Stock User"])
		sync(user, "test", {("Plant", "AICHI-VA"): True})
		visible = None
		with as_user(user):
			visible = frappe.get_list("Plant", pluck="name")
			warehouses = frappe.get_list("Warehouse", filters={"is_group": 0, "automotive_plant": ("is", "set")},
				pluck="automotive_plant")
		self.assertEqual(visible, ["AICHI-VA"])
		self.assertEqual(set(warehouses), {"AICHI-VA"})


class TestEffectiveDatedAccess(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		user = make_user("kyushu.super@hinode.example", ["Production Supervisor"])
		cls.employee = frappe.get_doc(
			{"doctype": "Employee", "first_name": "Aoi", "last_name": "Kuroda", "gender": "Female",
			 "date_of_birth": "1990-01-01", "date_of_joining": "2020-04-01", "company": KYUSHU, "user_id": user}
		).insert(ignore_permissions=True)

	def test_active_assignment_grants_and_ending_revokes(self):
		doc = frappe.get_doc(
			{"doctype": "Plant Assignment", "employee": self.employee.name, "plant": "KYUSHU-PT",
			 "assignment_role": "Supervisor", "from_date": add_days(nowdate(), -10)}
		).insert(ignore_permissions=True)
		self.assertEqual(doc.status, "Active")
		user = "kyushu.super@hinode.example"
		self.assertTrue(frappe.db.exists("User Permission", {"user": user, "allow": "Plant", "for_value": "KYUSHU-PT"}))
		self.assertTrue(frappe.db.exists("User Permission", {"user": user, "allow": "Company", "for_value": KYUSHU}))
		self.assertEqual(frappe.db.get_value("Employee", self.employee.name, "automotive_plant"), "KYUSHU-PT")
		doc.to_date = add_days(nowdate(), -1)
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.status, "Ended")
		self.assertFalse(frappe.db.exists("User Permission", {"user": user, "allow": "Plant", "for_value": "KYUSHU-PT"}))

	def test_future_assignment_is_scheduled_until_its_start(self):
		doc = frappe.get_doc(
			{"doctype": "Plant Assignment", "employee": self.employee.name, "plant": "KYUSHU-PT",
			 "assignment_role": "Supervisor", "from_date": add_days(nowdate(), 3)}
		).insert(ignore_permissions=True)
		self.assertEqual(doc.status, "Scheduled")
		self.assertFalse(frappe.db.exists("User Permission", {"user": "kyushu.super@hinode.example", "allow": "Plant"}))


class TestEmployeeScope(IntegrationTestCase):
	def test_linking_an_employee_does_not_add_company_scope(self):
		"""Scope comes from provisioning and plant assignments (D-007): ERPNext's automatic
		"own company" permission would confine a group-level user to one company."""
		user = make_user("buyer.group.scope@hinode.example", ["Buyer"])
		frappe.get_doc({"doctype": "Employee", "first_name": "Group", "last_name": "Buyer",
			"gender": frappe.db.get_value("Gender", {}, "name"), "date_of_birth": "1985-04-01",
			"date_of_joining": "2020-04-01", "company": AICHI, "user_id": user, "status": "Active",
			"create_user_permission": 1}).insert(ignore_permissions=True)
		self.assertFalse(frappe.db.exists("User Permission", {"user": user, "allow": "Company"}))
		self.assertFalse(frappe.db.exists("User Permission", {"user": user, "allow": "Employee"}))


class TestPartnerAccess(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.supplier = frappe.get_doc({"doctype": "Supplier", "supplier_name": "Akatsuki Fastener Works",
			"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")}).insert(ignore_permissions=True)
		cls.other = frappe.get_doc({"doctype": "Supplier", "supplier_name": "Sekiryu Castings",
			"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")}).insert(ignore_permissions=True)
		make_user("portal.akatsuki@akatsuki.example", [], user_type="Website User")

	def test_partner_access_links_contact_roles_and_scope(self):
		frappe.get_doc({"doctype": "Partner User Access", "user": "portal.akatsuki@akatsuki.example",
			"party_type": "Supplier", "supplier": self.supplier.name, "partner_role": "Logistics",
			"from_date": nowdate()}).insert(ignore_permissions=True)
		roles = frappe.get_roles("portal.akatsuki@akatsuki.example")
		self.assertIn("Supplier Portal User", roles)
		contact = frappe.db.get_value("Contact", {"user": "portal.akatsuki@akatsuki.example"}, "name")
		links = frappe.get_all("Dynamic Link", filters={"parent": contact, "parenttype": "Contact"},
			fields=["link_doctype", "link_name"])
		self.assertIn(frappe._dict(link_doctype="Supplier", link_name=self.supplier.name), links)
		self.assertTrue(frappe.db.exists("User Permission", {"user": "portal.akatsuki@akatsuki.example",
			"allow": "Supplier", "for_value": self.supplier.name}))
		with self.assertRaises(frappe.ValidationError):  # one partner per portal user
			frappe.get_doc({"doctype": "Partner User Access", "user": "portal.akatsuki@akatsuki.example",
				"party_type": "Supplier", "supplier": self.other.name, "partner_role": "Viewer",
				"from_date": nowdate()}).insert(ignore_permissions=True)


class TestMasterData(IntegrationTestCase):
	def test_tax_id_and_near_duplicate_names_are_blocked(self):
		group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		frappe.get_doc({"doctype": "Supplier", "supplier_name": "Hoshizora Electronics Co., Ltd.", "supplier_group": group,
			"tax_id": "JP-T1234567890123"}).insert(ignore_permissions=True)
		with self.assertRaises(DuplicateMasterDataError):
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "Totally Different KK", "supplier_group": group,
				"tax_id": "JP-T1234567890123"}).insert(ignore_permissions=True)
		with self.assertRaises(DuplicateMasterDataError):
			frappe.get_doc({"doctype": "Supplier", "supplier_name": "HOSHIZORA ELECTRONICS", "supplier_group": group}).insert(
				ignore_permissions=True)


class TestBankAccountControls(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user("ap.clerk@hinode.example", ["AP Clerk", "Accounts User"])
		make_user("treasury.one@hinode.example", ["Treasury Approver"])
		if not frappe.db.exists("Bank", "Hinode Test Bank"):
			frappe.get_doc({"doctype": "Bank", "bank_name": "Hinode Test Bank"}).insert(ignore_permissions=True)
		group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		cls.supplier = frappe.get_doc({"doctype": "Supplier", "supplier_name": "Kagerou Glass Industries",
			"supplier_group": group}).insert(ignore_permissions=True)

	def test_changed_account_needs_independent_verification(self):
		with as_user("ap.clerk@hinode.example"):
			account = frappe.get_doc({"doctype": "Bank Account", "account_name": "Kagerou main", "bank": "Hinode Test Bank",
				"party_type": "Supplier", "party": self.supplier.name, "bank_account_no": "1234567"}).insert()
		self.assertEqual(account.automotive_verification_status, "Pending Verification")
		with as_user("ap.clerk@hinode.example"), self.assertRaises(frappe.PermissionError):
			bank_accounts.verify(account.name)  # no Treasury role
		make_user("ap.clerk@hinode.example", ["AP Clerk", "Accounts User"])
		with as_user("treasury.one@hinode.example"):
			self.assertEqual(bank_accounts.verify(account.name)["status"], "Verified")
		with as_user("ap.clerk@hinode.example"):
			account.reload()
			account.bank_account_no = "7654321"
			account.save()
		self.assertEqual(account.automotive_verification_status, "Pending Verification")
		self.assertTrue(frappe.db.exists("Audit Event", {"event_type": "bank_account.changed", "reference_name": account.name}))

	def test_restricted_fields_are_permlevel_one(self):
		meta = frappe.get_meta("Bank Account")
		self.assertEqual(meta.get_field("bank_account_no").permlevel, 1)
