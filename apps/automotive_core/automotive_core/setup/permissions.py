"""Least-privilege grants for persona roles on standard ERPNext DocTypes (ERP-10).

Persona roles get exactly the rights listed here on standard DocTypes; everything
else comes from the ERPNext roles bundled in their Role Profile (setup/roles.py).
Grants are written as Custom DocPerm rows (the supported Role Permission Manager
mechanism) and re-applied idempotently on every migrate.

Field-level restrictions use permlevel property setters: supplier bank account
numbers are permlevel 1 and readable only by finance roles, an employee's personal
details only by HR. Lists, reports, exports and the REST API leave out the fields
of a level the user cannot read.
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter
from frappe.permissions import add_permission

READ = ("read", "report", "print")
WRITE = ("read", "write", "create", "report", "print")
SUBMIT = ("read", "write", "create", "submit", "report", "print")

GRANTS: list[tuple[str, str, tuple[str, ...]]] = [
	# Line operators: execute job cards and record downtime; read context only.
	("Job Card", "Line Operator", ("read", "write", "report", "print")),
	("Work Order", "Line Operator", READ),
	("Workstation", "Line Operator", READ),
	("Operation", "Line Operator", READ),
	("Item", "Line Operator", READ),
	("Warehouse", "Line Operator", READ),
	("Batch", "Line Operator", READ),
	("Serial No", "Line Operator", READ),
	("Downtime Entry", "Line Operator", WRITE),
	# Supervisors approve operator work and record downtime / handovers; posting a build of a
	# batch-tracked module creates its production batch.
	("Downtime Entry", "Production Supervisor", SUBMIT),
	("Batch", "Production Supervisor", WRITE),
	# Quality inspectors: inspections and nonconformance capture.
	("Quality Inspection", "Quality Inspector", SUBMIT),
	("Quality Inspection Template", "Quality Inspector", READ),
	("Non Conformance", "Quality Inspector", WRITE),
	("Item", "Quality Inspector", READ),
	("Batch", "Quality Inspector", READ),
	("Serial No", "Quality Inspector", READ),
	("Purchase Receipt", "Quality Inspector", READ),
	("Stock Entry", "Quality Inspector", READ),
	("Work Order", "Quality Inspector", READ),
	("Job Card", "Quality Inspector", READ),
	("Delivery Note", "Quality Inspector", READ),
	("Warehouse", "Quality Inspector", READ),
	("Supplier", "Quality Inspector", READ),
	("Quality Inspection Template", "Quality Manager", WRITE),
	# Dealer support: requests raised in the dealer portal are worked by the dealer liaisons.
	("Issue", "Dealer Liaison", WRITE),
	# Product engineering: items, BOMs and routings (approval through ECO).
	("Item", "Product Engineer", WRITE),
	("Item Attribute", "Product Engineer", WRITE),
	("Item Alternative", "Product Engineer", WRITE),
	("BOM", "Product Engineer", WRITE),
	("Routing", "Product Engineer", WRITE),
	("Operation", "Product Engineer", WRITE),
	("Workstation", "Product Engineer", READ),
	("Work Order", "Product Engineer", READ),
	("Supplier", "Product Engineer", READ),
	("Item", "Engineering Manager", WRITE),
	("BOM", "Engineering Manager", (*SUBMIT, "cancel", "amend")),
	# Treasury approvers review payments; they never create or submit them.
	("Payment Entry", "Treasury Approver", READ),
	("Purchase Invoice", "Treasury Approver", READ),
	("Supplier", "Treasury Approver", READ),
	("Bank Account", "Treasury Approver", READ),
	("Bank", "Treasury Approver", READ),
	("Bank Account", "AP Clerk", WRITE),
	# AP matches invoices against the orders (ERPNext's Accounts User reads receipts, not orders)
	("Purchase Order", "AP Clerk", READ),
	# AP books import costs (freight, duty) into the received goods' valuation
	("Landed Cost Voucher", "AP Clerk", SUBMIT),
	# Procurement: blanket (scheduling) agreements are negotiated by buyers and approved by
	# procurement managers; ERPNext itself grants purchase roles only "select" on them.
	("Blanket Order", "Buyer", SUBMIT),
	("Blanket Order", "Procurement Manager", (*SUBMIT, "cancel", "amend")),
	("Blanket Order", "Production Planner", READ),
	("Blanket Order", "AP Clerk", READ),
	# Buyers issue RFQs (ERPNext reserves submit for Purchase Manager).
	("Request for Quotation", "Buyer", SUBMIT),
	("Request for Quotation", "Procurement Manager", (*SUBMIT, "cancel", "amend")),
	# Receiving: ERPNext validates the supplier's payable account currency on every receipt,
	# which needs "select" on Account (ERPNext grants it to Stock Manager but not Stock User).
	("Account", "Warehouse Clerk", ("select",)),
	# receiving registers supplier lots as batches
	("Batch", "Warehouse Clerk", WRITE),
	# storage: logistics and the plant director set where parts are put away (ERPNext
	# reserves Putaway Rule for Stock Manager)
	("Putaway Rule", "Logistics Coordinator", WRITE),
	("Putaway Rule", "Plant Director", WRITE),
	# Logistics: shipments and dispatch.
	("Shipment", "Logistics Coordinator", SUBMIT),
	("Delivery Note", "Logistics Coordinator", READ),
	# Executives: read-only management visibility.
	*[
		(doctype, "Automotive Executive", READ)
		for doctype in (
			"Sales Order",
			"Sales Invoice",
			"Purchase Order",
			"Purchase Invoice",
			"Work Order",
			"Stock Entry",
			"Item",
			"Customer",
			"Supplier",
			"Warehouse",
			"Company",
			"GL Entry",
			"Quality Inspection",
		)
	],
	# Auditors: read across operations (ERPNext's Auditor covers accounting).
	*[
		(doctype, "Auditor", READ)
		for doctype in (
			"Stock Entry",
			"Purchase Receipt",
			"Delivery Note",
			"Work Order",
			"Job Card",
			"Quality Inspection",
			"BOM",
			"Purchase Order",
			"Sales Order",
			"Material Request",
			"Pick List",
			"Putaway Rule",
			"Item",
			"Warehouse",
			"Stock Reconciliation",
			"Landed Cost Voucher",
			"Non Conformance",
			"Blanket Order",
			"Stock Reservation Entry",
			"Serial No",
			"Batch",
			"Serial and Batch Bundle",
			"Stock Ledger Entry",
			"Bin",
			# ERPNext's Auditor reads purchase invoices and journals but not these
			"Sales Invoice",
			"Payment Entry",
		)
	],
]

# An employee's personal details, terms of employment, pay and exit (SEC-4 data minimization).
# ERPNext gives every user linked to an Employee the "Employee" role, which reads all employees:
# colleagues keep the directory (name, company, department, designation, plant, reports to, user,
# company email), HR the rest.
EMPLOYEE_PERSONAL_FIELDS = (
	"gender",
	"date_of_birth",
	"emergency_phone_number",
	"person_to_be_contacted",
	"relation",
	"scheduled_confirmation_date",
	"final_confirmation_date",
	"contract_end_date",
	"notice_number_of_days",
	"date_of_retirement",
	"salary_mode",
	"bank_name",
	"bank_ac_no",
	"iban",
	"salary_currency",
	"ctc",
	"cell_number",
	"prefered_email",
	"personal_email",
	"permanent_accommodation_type",
	"permanent_address",
	"current_accommodation_type",
	"current_address",
	"passport_number",
	"date_of_issue",
	"valid_upto",
	"place_of_issue",
	"marital_status",
	"blood_group",
	"family_background",
	"health_details",
	"education",
	"external_work_history",
	"internal_work_history",
	"resignation_letter_date",
	"relieving_date",
	"reason_for_leaving",
	"leave_encashed",
	"encashment_date",
	"held_on",
	"new_workplace",
	"feedback",
)
# (doctype, fieldname): restricted to permlevel 1
RESTRICTED_FIELDS = [
	("Bank Account", "bank_account_no"),
	("Bank Account", "iban"),
	("Bank Account", "branch_code"),
	*(("Employee", fieldname) for fieldname in EMPLOYEE_PERSONAL_FIELDS),
]
PERMLEVEL_1 = [
	("Bank Account", "Treasury Approver", ("read",)),
	("Bank Account", "Group Controller", ("read",)),
	("Bank Account", "Accounts Manager", ("read", "write")),
	("Bank Account", "AP Clerk", ("read", "write")),
	("Bank Account", "System Manager", ("read", "write")),
	# ERPNext's HR roles, held by HR staff (no persona of this ERP): not System Manager either
	("Employee", "HR Manager", ("read", "write")),
	("Employee", "HR User", ("read", "write")),
]


RIGHT_FIELDS = (
	"read",
	"write",
	"create",
	"delete",
	"submit",
	"cancel",
	"amend",
	"report",
	"export",
	"import",
	"share",
	"print",
	"email",
	"select",
	"mask",
)


def _grant(doctype: str, role: str, ptypes: tuple[str, ...], permlevel: int = 0) -> None:
	"""The role's rule on the DocType holds the rights listed. A rule that ERPNext's own
	permissions have as well keeps their rights and gains these; a rule only these grants make
	holds exactly them (a new Custom DocPerm reads and exports by default: SEC-4)."""
	if not frappe.db.exists("DocType", doctype) or not frappe.db.exists("Role", role):
		return
	where = {"parent": doctype, "role": role, "permlevel": permlevel, "if_owner": 0}
	if not frappe.db.exists("Custom DocPerm", where):
		add_permission(doctype, role, permlevel)
	name = frappe.db.get_value("Custom DocPerm", where, "name")
	shared = frappe.db.exists("DocPerm", where)
	meta = frappe.get_meta("Custom DocPerm")
	rights = [p for p in RIGHT_FIELDS if meta.has_field(p)]
	current = frappe.db.get_value("Custom DocPerm", name, rights, as_dict=True)
	wanted = {p: int(p in ptypes or bool(shared and current[p])) for p in rights}
	changed = {p: v for p, v in wanted.items() if int(current[p] or 0) != v}
	if changed:
		frappe.db.set_value("Custom DocPerm", name, changed, update_modified=False)


def apply() -> None:
	for doctype, role, ptypes in GRANTS:
		_grant(doctype, role, ptypes)
	for doctype, fieldname in RESTRICTED_FIELDS:
		if frappe.get_meta(doctype).has_field(fieldname):
			make_property_setter(doctype, fieldname, "permlevel", 1, "Int", validate_fields_for_doctype=False)
	for doctype, role, ptypes in PERMLEVEL_1:
		_grant(doctype, role, ptypes, permlevel=1)
	frappe.clear_cache()


def report_roles(doctype: str) -> set[str]:
	"""Roles that may run reports on ``doctype`` (the "report" right at level 0), read as Frappe
	reads them: from its Custom DocPerms when it has any, else from its DocPerms. A right on the
	user's own documents only (*If Owner*) does not run a report over the DocType."""
	table = "Custom DocPerm" if frappe.db.exists("Custom DocPerm", {"parent": doctype}) else "DocPerm"
	return set(
		frappe.get_all(
			table, filters={"parent": doctype, "permlevel": 0, "report": 1, "if_owner": 0}, pluck="role"
		)
	)


def align_report_roles() -> list[str]:
	"""Workspaces and sidebars offer the reports a user's roles may open, but a report runs only
	for those who may report on its DocType: for anyone else its link answers "no permission"
	(ERP-12.2, GATE-1). Each report gets a Custom Role naming the roles that keep it: of the roles
	it names (all, when it names none), those that can run it, plus, for every role profile that
	could open it and holds a role that can run it, that role, so the profile keeps the report.
	A report naming no roles of its own also gets them as its own rows: Frappe offers such a
	report to everyone, Custom Role or not. Re-applied on every migrate, so reports that upgrades
	add or re-sync are aligned too. Returns the reports changed."""
	from automotive_core.setup.roles import ROLE_PROFILES

	profiles = [set(roles) for roles in ROLE_PROFILES.values()]
	changed = []
	for report in frappe.get_all(
		"Report", filters={"disabled": 0}, fields=["name", "ref_doctype"], order_by="name"
	):
		if not report.ref_doctype or not frappe.db.exists("DocType", report.ref_doctype):
			continue
		able = report_roles(report.ref_doctype)
		if "All" in able:
			continue
		custom = frappe.db.get_value("Custom Role", {"report": report.name}, "name")
		own = set(
			frappe.get_all("Has Role", filters={"parenttype": "Report", "parent": report.name}, pluck="role")
		)
		current = (
			set(
				frappe.get_all(
					"Has Role", filters={"parenttype": "Custom Role", "parent": custom}, pluck="role"
				)
			)
			if custom
			else own
		)
		keep = (current & able) if current else set(able)
		for mine in profiles:
			if (not current or mine & current) and mine & able:
				keep |= mine & able
		# a profile that opens the report through a kept role keeps it through all its roles that
		# can run it: the result is stable, so the next migrate changes nothing
		grown = True
		while grown:
			grown = False
			for mine in profiles:
				if mine & keep and not (mine & able) <= keep:
					keep |= mine & able
					grown = True
		# an empty Custom Role would not hide the report: Administrator keeps it
		wanted = keep or {"Administrator"}
		if wanted != current:
			doc = frappe.get_doc("Custom Role", custom) if custom else frappe.new_doc("Custom Role")
			doc.update({"report": report.name, "ref_doctype": report.ref_doctype})
			doc.set("roles", [{"role": role} for role in sorted(wanted)])
			doc.flags.ignore_permissions = True
			doc.save()
			changed.append(report.name)
		if not own:
			for role in sorted(wanted):
				frappe.get_doc(
					{
						"doctype": "Has Role",
						"parent": report.name,
						"parenttype": "Report",
						"parentfield": "roles",
						"role": role,
					}
				).db_insert()
			if report.name not in changed:
				changed.append(report.name)
	return changed
