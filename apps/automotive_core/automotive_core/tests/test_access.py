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
		roles = set(
			frappe.get_all(
				"Warehouse", filters={"automotive_plant": "AICHI-VA"}, pluck="automotive_warehouse_role"
			)
		)
		for role in (
			"Receiving",
			"Quarantine",
			"Line Side",
			"Work In Progress",
			"Finished Goods",
			"In Transit",
			"Consignment",
			"Outbound",
		):
			self.assertIn(role, roles)
		self.assertEqual(frappe.db.get_value("Company", NA, "default_currency"), "USD")
		# the month-end revaluation of foreign-currency balances posts to this account
		for company in (AICHI, KYUSHU, NA):
			account = frappe.db.get_value("Company", company, "unrealized_exchange_gain_loss_account")
			self.assertEqual(frappe.db.get_value("Account", account, "company"), company)
		self.assertTrue(
			frappe.db.exists("Customer", {"is_internal_customer": 1, "represents_company": AICHI})
		)
		line = frappe.get_doc("Production Line", "AICHI-FA1")
		self.assertEqual(line.max_units_per_day, (960 * 60 // 64))
		self.assertEqual(len(line.stations), 5)
		# ENT-4: the eleven functions are departments of each company
		functions = {
			"Engineering",
			"Production Planning",
			"Plant Operations",
			"Warehousing",
			"Quality",
			"Purchasing",
			"Finance",
			"Treasury",
			"Sales",
			"Logistics",
			"IT",
		}
		for company in (AICHI, KYUSHU, NA):
			departments = set(
				frappe.get_all("Department", filters={"company": company}, pluck="department_name")
			)
			self.assertLessEqual(functions, departments, company)

	def test_holiday_calendars_include_plant_shutdowns(self):
		holidays = frappe.get_all(
			"Holiday", filters={"parent": "JP 2026 Plant Calendar"}, pluck="holiday_date"
		)
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

		user = make_user(
			"aichi.planner@hinode.example", ["Production Planner", "Manufacturing User", "Stock User"]
		)
		sync(user, "test", {("Plant", "AICHI-VA"): True})
		visible = None
		with as_user(user):
			visible = frappe.get_list("Plant", pluck="name")
			warehouses = frappe.get_list(
				"Warehouse",
				filters={"is_group": 0, "automotive_plant": ("is", "set")},
				pluck="automotive_plant",
			)
		self.assertEqual(visible, ["AICHI-VA"])
		self.assertEqual(set(warehouses), {"AICHI-VA"})


class TestEffectiveDatedAccess(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		user = make_user("kyushu.super@hinode.example", ["Production Supervisor"])
		cls.employee = frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "Aoi",
				"last_name": "Kuroda",
				"gender": "Female",
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-04-01",
				"company": KYUSHU,
				"user_id": user,
			}
		).insert(ignore_permissions=True)

	def test_active_assignment_grants_and_ending_revokes(self):
		doc = frappe.get_doc(
			{
				"doctype": "Plant Assignment",
				"employee": self.employee.name,
				"plant": "KYUSHU-PT",
				"assignment_role": "Supervisor",
				"from_date": add_days(nowdate(), -10),
			}
		).insert(ignore_permissions=True)
		self.assertEqual(doc.status, "Active")
		user = "kyushu.super@hinode.example"
		self.assertTrue(
			frappe.db.exists("User Permission", {"user": user, "allow": "Plant", "for_value": "KYUSHU-PT"})
		)
		self.assertTrue(
			frappe.db.exists("User Permission", {"user": user, "allow": "Company", "for_value": KYUSHU})
		)
		self.assertEqual(frappe.db.get_value("Employee", self.employee.name, "automotive_plant"), "KYUSHU-PT")
		doc.to_date = add_days(nowdate(), -1)
		doc.save(ignore_permissions=True)
		self.assertEqual(doc.status, "Ended")
		self.assertFalse(
			frappe.db.exists("User Permission", {"user": user, "allow": "Plant", "for_value": "KYUSHU-PT"})
		)

	def test_future_assignment_is_scheduled_until_its_start(self):
		doc = frappe.get_doc(
			{
				"doctype": "Plant Assignment",
				"employee": self.employee.name,
				"plant": "KYUSHU-PT",
				"assignment_role": "Supervisor",
				"from_date": add_days(nowdate(), 3),
			}
		).insert(ignore_permissions=True)
		self.assertEqual(doc.status, "Scheduled")
		self.assertFalse(
			frappe.db.exists("User Permission", {"user": "kyushu.super@hinode.example", "allow": "Plant"})
		)


class TestEmployeeScope(IntegrationTestCase):
	def test_linking_an_employee_does_not_add_company_scope(self):
		"""Scope comes from provisioning and plant assignments (D-007): ERPNext's automatic
		"own company" permission would confine a group-level user to one company."""
		user = make_user("buyer.group.scope@hinode.example", ["Buyer"])
		frappe.get_doc(
			{
				"doctype": "Employee",
				"first_name": "Group",
				"last_name": "Buyer",
				"gender": frappe.db.get_value("Gender", {}, "name"),
				"date_of_birth": "1985-04-01",
				"date_of_joining": "2020-04-01",
				"company": AICHI,
				"user_id": user,
				"status": "Active",
				"create_user_permission": 1,
			}
		).insert(ignore_permissions=True)
		self.assertFalse(frappe.db.exists("User Permission", {"user": user, "allow": "Company"}))
		self.assertFalse(frappe.db.exists("User Permission", {"user": user, "allow": "Employee"}))


class TestPartnerAccess(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.supplier = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "Akatsuki Fastener Works",
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"),
			}
		).insert(ignore_permissions=True)
		cls.other = frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "Sekiryu Castings",
				"supplier_group": frappe.db.get_value("Supplier Group", {"is_group": 0}, "name"),
			}
		).insert(ignore_permissions=True)
		make_user("portal.akatsuki@akatsuki.example", [], user_type="Website User")

	def test_partner_access_links_contact_roles_and_scope(self):
		frappe.get_doc(
			{
				"doctype": "Partner User Access",
				"user": "portal.akatsuki@akatsuki.example",
				"party_type": "Supplier",
				"supplier": self.supplier.name,
				"partner_role": "Logistics",
				"from_date": nowdate(),
			}
		).insert(ignore_permissions=True)
		roles = frappe.get_roles("portal.akatsuki@akatsuki.example")
		self.assertIn("Supplier Portal User", roles)
		contact = frappe.db.get_value("Contact", {"user": "portal.akatsuki@akatsuki.example"}, "name")
		links = frappe.get_all(
			"Dynamic Link",
			filters={"parent": contact, "parenttype": "Contact"},
			fields=["link_doctype", "link_name"],
		)
		self.assertIn(frappe._dict(link_doctype="Supplier", link_name=self.supplier.name), links)
		self.assertTrue(
			frappe.db.exists(
				"User Permission",
				{
					"user": "portal.akatsuki@akatsuki.example",
					"allow": "Supplier",
					"for_value": self.supplier.name,
				},
			)
		)
		with self.assertRaises(frappe.ValidationError):  # one partner per portal user
			frappe.get_doc(
				{
					"doctype": "Partner User Access",
					"user": "portal.akatsuki@akatsuki.example",
					"party_type": "Supplier",
					"supplier": self.other.name,
					"partner_role": "Viewer",
					"from_date": nowdate(),
				}
			).insert(ignore_permissions=True)


class TestMasterData(IntegrationTestCase):
	def test_tax_id_and_near_duplicate_names_are_blocked(self):
		group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		frappe.get_doc(
			{
				"doctype": "Supplier",
				"supplier_name": "Hoshizora Electronics Co., Ltd.",
				"supplier_group": group,
				"tax_id": "JP-T1234567890123",
			}
		).insert(ignore_permissions=True)
		with self.assertRaises(DuplicateMasterDataError):
			frappe.get_doc(
				{
					"doctype": "Supplier",
					"supplier_name": "Totally Different KK",
					"supplier_group": group,
					"tax_id": "JP-T1234567890123",
				}
			).insert(ignore_permissions=True)
		with self.assertRaises(DuplicateMasterDataError):
			frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": "HOSHIZORA ELECTRONICS", "supplier_group": group}
			).insert(ignore_permissions=True)

	def test_items_are_unique_by_supplier_part_number_and_by_name_in_their_group(self):
		group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		supplier = (
			frappe.get_doc(
				{"doctype": "Supplier", "supplier_name": "Akatsuki Fastener Works", "supplier_group": group}
			)
			.insert(ignore_permissions=True)
			.name
		)

		def item(code, name, group="Raw Material", part_no=None, **extra):
			return frappe.get_doc(
				{
					"doctype": "Item",
					"item_code": code,
					"item_name": name,
					"item_group": group,
					"stock_uom": "Nos",
					"is_stock_item": 1,
					**extra,
					"supplier_items": [{"supplier": supplier, "supplier_part_no": part_no}]
					if part_no
					else [],
				}
			)

		item("DUP-BRACKET-1", "Hinge bracket 40 mm", part_no="AFW-4410").insert(ignore_permissions=True)
		with self.assertRaises(DuplicateMasterDataError):  # the supplier's part number is taken
			item("DUP-BRACKET-2", "Hinge bracket 40 mm zinc", part_no="AFW-4410").insert(
				ignore_permissions=True
			)
		with self.assertRaises(DuplicateMasterDataError):  # same name in the same group
			item("DUP-BRACKET-3", "Hinge bracket 40 mm").insert(ignore_permissions=True)
		item("DUP-BRACKET-4", "Hinge bracket 40 mm", group="Sub Assemblies").insert(ignore_permissions=True)
		reviewed = item("DUP-BRACKET-5", "Hinge bracket 40 mm")
		reviewed.flags.accept_similar_name = True
		reviewed.insert(ignore_permissions=True)
		self.assertTrue(
			frappe.db.exists(
				"Audit Event",
				{"event_type": "master_data.similar_name_accepted", "reference_name": "DUP-BRACKET-5"},
			)
		)


class TestBankAccountControls(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		make_user("ap.clerk@hinode.example", ["AP Clerk", "Accounts User"])
		make_user("treasury.one@hinode.example", ["Treasury Approver"])
		if not frappe.db.exists("Bank", "Hinode Test Bank"):
			frappe.get_doc({"doctype": "Bank", "bank_name": "Hinode Test Bank"}).insert(
				ignore_permissions=True
			)
		group = frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		cls.supplier = frappe.get_doc(
			{"doctype": "Supplier", "supplier_name": "Kagerou Glass Industries", "supplier_group": group}
		).insert(ignore_permissions=True)

	def test_changed_account_needs_independent_verification(self):
		with as_user("ap.clerk@hinode.example"):
			account = frappe.get_doc(
				{
					"doctype": "Bank Account",
					"account_name": "Kagerou main",
					"bank": "Hinode Test Bank",
					"party_type": "Supplier",
					"party": self.supplier.name,
					"bank_account_no": "1234567",
				}
			).insert()
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
		self.assertTrue(
			frappe.db.exists(
				"Audit Event", {"event_type": "bank_account.changed", "reference_name": account.name}
			)
		)

	def test_restricted_fields_are_permlevel_one(self):
		meta = frappe.get_meta("Bank Account")
		self.assertEqual(meta.get_field("bank_account_no").permlevel, 1)


class TestDataMinimization(IntegrationTestCase):
	"""SEC-4: restricted fields (supplier bank details, an employee's personal details) reach only
	the roles that need them, in every read path: the form and the REST API, lists, and exports,
	which also need the export right and are recorded in the Access Log. No report, page or API
	of the production apps uses a restricted field except the reviewed finance controls."""

	EXPORTER = "Minimization Test Exporter"
	PERSONAL = {
		"cell_number": "090-0000-0001",
		"personal_email": "subject.private@example.invalid",
		"current_address": "1-2-3 Example-cho, Toyota",
		"passport_number": "TZ0000001",
		"bank_ac_no": "0000001",
		"health_details": "synthetic test record",
	}

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.addClassCleanup(cls._drop_exporter)
		gender = frappe.db.get_value("Gender", {}, "name")
		cls.subject = cls._employee("Subject", None, gender, cls.PERSONAL)
		# linking a user to an Employee gives it ERPNext's "Employee" role, which reads all employees
		cls.colleague = make_user("minimization.colleague@hinode.example", ["Line Operator"])
		cls._employee("Colleague", cls.colleague, gender, {})
		cls.hr_roles = sorted(r for r in ("HR Manager", "HR User") if frappe.db.exists("Role", r))

	@staticmethod
	def _employee(last_name, user, gender, values):
		name = frappe.db.get_value("Employee", {"first_name": "Minimization", "last_name": last_name})
		doc = frappe.get_doc("Employee", name) if name else frappe.new_doc("Employee")
		doc.update(
			{
				"first_name": "Minimization",
				"last_name": last_name,
				"company": AICHI,
				"status": "Active",
				"gender": gender,
				"date_of_birth": "1990-01-01",
				"date_of_joining": "2020-04-01",
				"user_id": user,
				**values,
			}
		)
		doc.flags.ignore_permissions = True
		doc.save()
		return doc.name

	@classmethod
	def _drop_exporter(cls):
		frappe.db.delete("Custom DocPerm", {"parent": "Employee", "role": cls.EXPORTER})
		frappe.db.delete("Has Role", {"role": cls.EXPORTER})
		frappe.db.delete("Role", cls.EXPORTER)
		frappe.clear_cache(doctype="Employee")

	def test_the_restricted_fields_are_read_only_by_the_named_roles(self):
		from automotive_core.setup.permissions import PERMLEVEL_1, RESTRICTED_FIELDS
		from automotive_core.setup.roles import ROLE_PROFILES

		persona_roles = {role for roles in ROLE_PROFILES.values() for role in roles}
		for doctype in sorted({doctype for doctype, _ in RESTRICTED_FIELDS}):
			meta = frappe.get_meta(doctype)
			for dt, fieldname in RESTRICTED_FIELDS:
				if dt == doctype and meta.has_field(fieldname):
					self.assertEqual(meta.get_field(fieldname).permlevel, 1, f"{doctype}.{fieldname}")
			readers = set(
				frappe.get_all(
					"Custom DocPerm", filters={"parent": doctype, "permlevel": 1, "read": 1}, pluck="role"
				)
			)
			named = {role for dt, role, _ in PERMLEVEL_1 if dt == doctype and frappe.db.exists("Role", role)}
			self.assertEqual(readers, named, doctype)
			# Data Export reads child tables past their table field's level: only its readers export
			if any(
				dt == doctype and meta.has_field(f) and meta.get_field(f).fieldtype == "Table"
				for dt, f in RESTRICTED_FIELDS
			):
				exporters = set(
					frappe.get_all(
						"Custom DocPerm",
						filters={"parent": doctype, "permlevel": 0, "export": 1},
						pluck="role",
					)
				)
				self.assertLessEqual(exporters, readers, doctype)
		self.assertTrue(self.hr_roles, "ERPNext's HR roles")
		self.assertFalse(
			set(self.hr_roles) & persona_roles, "no persona reads an employee's personal details"
		)

	def test_a_colleague_reads_the_directory_not_the_personal_details(self):
		self.assertIn("Employee", frappe.get_roles(self.colleague))
		with as_user(self.colleague):
			doc = frappe.client.get("Employee", self.subject)
			self.assertEqual(doc["employee_name"], "Minimization Subject")
			self.assertEqual(doc["company"], AICHI)
			for fieldname in (*self.PERSONAL, "date_of_birth", "gender"):
				self.assertFalse(doc.get(fieldname), fieldname)
			listed = frappe.get_list("Employee", filters={"name": self.subject}, fields=["*"])
			self.assertEqual(listed[0]["employee_name"], "Minimization Subject")
			self.assertFalse(set(listed[0]) & {*self.PERSONAL, "date_of_birth", "gender"})
			# the Employee role reads, but exports nothing
			self.assertFalse(frappe.has_permission("Employee", "export"))
		hr = make_user("minimization.hr@hinode.example", self.hr_roles)
		with as_user(hr):
			doc = frappe.client.get("Employee", self.subject)
			for fieldname, value in self.PERSONAL.items():
				self.assertEqual(doc.get(fieldname), value, fieldname)

	def _export(self, user, fields):
		from frappe.desk.reportview import export_query

		frappe.local.form_dict = frappe._dict(
			doctype="Employee",
			fields=frappe.as_json([f"`tabEmployee`.`{fieldname}`" for fieldname in fields]),
			filters=frappe.as_json([["Employee", "name", "=", self.subject]]),
			file_format_type="CSV",
			title="Employee",
		)
		frappe.response.pop("filecontent", None)
		with as_user(user):
			export_query()
		content = frappe.response.get("filecontent")
		return content.decode() if isinstance(content, bytes) else content or ""

	def test_an_export_leaves_out_what_the_user_cannot_read_and_is_logged(self):
		from frappe.permissions import add_permission, update_permission_property

		if not frappe.db.exists("Role", self.EXPORTER):
			frappe.get_doc({"doctype": "Role", "role_name": self.EXPORTER, "desk_access": 1}).insert(
				ignore_permissions=True
			)
		add_permission("Employee", self.EXPORTER, 0)
		for ptype in ("read", "report", "export"):
			update_permission_property("Employee", self.EXPORTER, 0, ptype, 1, validate=False)
		frappe.clear_cache(doctype="Employee")
		exporter = make_user("minimization.exporter@hinode.example", [self.EXPORTER])
		before = frappe.db.count("Access Log", {"user": exporter, "export_from": "Employee"})
		try:
			csv = self._export(exporter, ["name", "employee_name", "cell_number", "personal_email"])
		finally:
			self._drop_exporter()
		self.assertIn("Minimization Subject", csv)
		for value in self.PERSONAL.values():
			self.assertNotIn(value, csv)
		self.assertEqual(
			frappe.db.count("Access Log", {"user": exporter, "export_from": "Employee", "file_type": "CSV"}),
			before + 1,
		)
		# with the field level, the same export carries them
		csv = self._export(
			make_user("minimization.hr@hinode.example", self.hr_roles), ["name", "cell_number"]
		)
		self.assertIn(self.PERSONAL["cell_number"], csv)
		# without the export right, no export
		with self.assertRaises(frappe.PermissionError):
			self._export(self.colleague, ["name", "employee_name"])

	def test_a_report_download_is_logged_like_the_other_exports(self):
		report = "Segregation of Duties Review"
		target = frappe.get_hooks("override_whitelisted_methods", {})["frappe.desk.query_report.export_query"]
		download = frappe.get_attr(target[-1])
		before = frappe.db.count("Access Log", {"report_name": report})
		frappe.local.form_dict = frappe._dict(
			report_name=report, file_format_type="CSV", filters="{}", visible_idx="[]"
		)
		frappe.response.pop("filecontent", None)
		download()
		self.assertTrue(frappe.response.get("filecontent"))
		self.assertEqual(frappe.db.count("Access Log", {"report_name": report}), before + 1)
		log = frappe.get_last_doc("Access Log", filters={"report_name": report})
		self.assertEqual(
			(log.user, log.export_from, log.file_type),
			("Administrator", "Segregation of Duties Rule", "CSV"),
		)
		# without the export right on the report's DocType: refused, nothing logged
		frappe.local.form_dict = frappe._dict(report_name=report, file_format_type="CSV", filters="{}")
		with as_user(self.colleague), self.assertRaises(frappe.PermissionError):
			download()
		self.assertEqual(frappe.db.count("Access Log", {"report_name": report}), before + 1)

	def test_restricted_fields_are_used_only_where_reviewed(self):
		"""Reports, dashboards, pages, print formats, portals and APIs show only what they need: a new
		use of a restricted field fails here until it is reviewed and listed."""
		import re
		from pathlib import Path

		from automotive_core.setup.permissions import RESTRICTED_FIELDS

		reviewed = {
			# the bank account change control: verification status, audit event (values not shown)
			"automotive_core/finance_controls/bank_accounts.py",
			# inbound bank statements resolve the company account by its number
			"automotive_core/finance_controls/bank_statements.py",
			"automotive_core/setup/permissions.py",
		}
		names = sorted({fieldname for _, fieldname in RESTRICTED_FIELDS})
		pattern = re.compile(r"\b(" + "|".join(names) + r")\b")
		found = []
		for app in frappe.get_installed_apps():
			if not app.startswith("automotive_") or app == "automotive_seed":
				continue
			root = Path(frappe.get_app_path(app))
			for path in sorted(root.rglob("*")):
				relative = path.relative_to(root.parent).as_posix()
				if (
					not path.is_file()
					or path.suffix not in (".py", ".json", ".js", ".html", ".sql", ".vue", ".ts")
					or {"tests", "node_modules", "dist"} & set(path.parts)
					or relative in reviewed
				):
					continue
				for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
					if match := pattern.search(line):
						found.append(f"{relative}:{number} ({match.group(1)})")
		self.assertEqual(found, [])


class TestBackgroundJobs(IntegrationTestCase):
	"""P-4: a background job runs as the user who queued it, or as a system step that writes past
	the permissions (sealing, dispatch, inbound processing, scans). Either way only the methods
	that queue it may start it, and those check the caller: no queued job and no scheduled task
	of the production apps can be called over HTTP itself."""

	def test_jobs_and_scheduled_tasks_cannot_be_called_over_http(self):
		import ast
		from pathlib import Path

		apps = [
			app
			for app in frappe.get_installed_apps()
			if app.startswith("automotive_") and app != "automotive_seed"
		]
		self.assertGreaterEqual(len(apps), 4)
		targets = set()
		for app in apps:
			for path in Path(frappe.get_app_path(app)).rglob("*.py"):
				if "tests" in path.parts:
					continue
				for node in ast.walk(ast.parse(path.read_text())):
					if (
						isinstance(node, ast.Call)
						and ast.unparse(node.func) == "frappe.enqueue"
						and node.args
						and isinstance(node.args[0], ast.Constant)
					):
						targets.add(node.args[0].value)
			for event, paths in (frappe.get_hooks("scheduler_events", app_name=app) or {}).items():
				for path in (p for ps in paths.values() for p in ps) if event == "cron" else paths:
					targets.add(path)
		self.assertGreaterEqual(len(targets), 15)
		for target in sorted(targets):
			with self.subTest(target):
				self.assertNotIn(
					frappe.get_attr(target), frappe.whitelisted, f"{target} can be called over HTTP"
				)
