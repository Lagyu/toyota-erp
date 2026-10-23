"""Custom fields that automotive_core adds to standard Frappe/ERPNext DocTypes.

All fieldnames carry the ``automotive_`` prefix so that a future upstream field of
the same name cannot collide during an upgrade. Applied idempotently on every
migrate (create_custom_fields(update=True)).
"""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from automotive_core.utils import hook_list

WAREHOUSE_ROLES = [
	"",
	"Receiving",
	"Receiving Inspection",
	"Raw Material",
	"Line Side",
	"Work In Progress",
	"Quarantine",
	"Finished Goods",
	"Outbound",
	"In Transit",
	"Scrap",
	"Consignment",
	"Storage",
]


def plant_field(insert_after: str, **extra) -> dict:
	return {
		"fieldname": "automotive_plant",
		"label": "Plant",
		"fieldtype": "Link",
		"options": "Plant",
		"insert_after": insert_after,
		"in_standard_filter": 1,
		"search_index": 1,
		# mapped documents (e.g. an intercompany receipt from the seller's delivery) re-derive
		# their own plant from their warehouses instead of inheriting the source's
		"no_copy": 1,
		**extra,
	}


def core_custom_fields() -> dict[str, list[dict]]:
	fields: dict[str, list[dict]] = {
		"Warehouse": [
			plant_field("company"),
			{
				"fieldname": "automotive_warehouse_role",
				"label": "Warehouse Role",
				"fieldtype": "Select",
				"options": "\n".join(WAREHOUSE_ROLES),
				"insert_after": "automotive_plant",
				"in_standard_filter": 1,
			},
		],
		"Workstation": [
			plant_field("workstation_type"),
			{
				"fieldname": "automotive_production_line",
				"label": "Production Line",
				"fieldtype": "Link",
				"options": "Production Line",
				"insert_after": "automotive_plant",
			},
		],
		"Employee": [
			plant_field("department", read_only=1, description="Maintained from the active Plant Assignment."),
		],
		"User": [
			{
				"fieldname": "automotive_access_expiry_date",
				"label": "Access Expires On",
				"fieldtype": "Date",
				"insert_after": "enabled",
				"description": "The account is disabled automatically after this date.",
			},
		],
		"User Permission": [
			{
				"fieldname": "automotive_managed_by",
				"label": "Managed By",
				"fieldtype": "Data",
				"read_only": 1,
				"insert_after": "for_value",
				"description": "Access source that maintains this permission automatically.",
			},
		],
		"File": [
			{
				"fieldname": "automotive_sha256",
				"label": "SHA-256",
				"fieldtype": "Data",
				"read_only": 1,
				"insert_after": "content_hash",
			},
			{
				"fieldname": "automotive_scan_status",
				"label": "Malware Scan",
				"fieldtype": "Select",
				"options": "\nNot Scanned\nClean\nInfected\nScan Failed\nScanner Not Configured",
				"read_only": 1,
				"insert_after": "automotive_sha256",
			},
			{
				"fieldname": "automotive_scanned_at",
				"label": "Scanned At",
				"fieldtype": "Datetime",
				"read_only": 1,
				"insert_after": "automotive_scan_status",
			},
		],
		"Bank Account": [
			{
				"fieldname": "automotive_verification_section",
				"label": "Verification",
				"fieldtype": "Section Break",
				"insert_after": "is_default",
			},
			{
				"fieldname": "automotive_verification_status",
				"label": "Verification Status",
				"fieldtype": "Select",
				"options": "Unverified\nPending Verification\nVerified",
				"default": "Unverified",
				"read_only": 1,
				"in_list_view": 1,
				"insert_after": "automotive_verification_section",
			},
			{
				"fieldname": "automotive_account_fingerprint",
				"label": "Account Fingerprint",
				"fieldtype": "Data",
				"read_only": 1,
				"insert_after": "automotive_verification_status",
			},
			{
				"fieldname": "automotive_verified_by",
				"label": "Verified By",
				"fieldtype": "Link",
				"options": "User",
				"read_only": 1,
				"insert_after": "automotive_account_fingerprint",
			},
			{
				"fieldname": "automotive_verified_at",
				"label": "Verified At",
				"fieldtype": "Datetime",
				"read_only": 1,
				"insert_after": "automotive_verified_by",
			},
		],
		"Stock Entry": [
			{
				"fieldname": "automotive_source_doctype",
				"label": "Initiating DocType",
				"fieldtype": "Link",
				"options": "DocType",
				"read_only": 1,
				"insert_after": "automotive_plant",
				"no_copy": 1,
			},
			{
				"fieldname": "automotive_source_name",
				"label": "Initiating Document",
				"fieldtype": "Dynamic Link",
				"options": "automotive_source_doctype",
				"read_only": 1,
				"insert_after": "automotive_source_doctype",
				"no_copy": 1,
				"search_index": 1,
			},
		],
		"Supplier": [
			{
				"fieldname": "automotive_partner_code",
				"label": "Partner Code",
				"fieldtype": "Data",
				"search_index": 1,
				"insert_after": "supplier_name",
				"description": "Short code used in EDI messages and portal URLs.",
			},
		],
		"Customer": [
			{
				"fieldname": "automotive_partner_code",
				"label": "Partner Code",
				"fieldtype": "Data",
				"search_index": 1,
				"insert_after": "customer_name",
				"description": "Dealer code used in EDI messages and portal URLs.",
			},
		],
	}
	for doctype in hook_list("automotive_plant_scoped_doctypes"):
		meta = frappe.get_meta(doctype)
		anchor = "company" if meta.has_field("company") else (meta.fields[0].fieldname if meta.fields else None)
		fields.setdefault(doctype, []).insert(0, plant_field(anchor))
	return fields


def apply() -> None:
	create_custom_fields(core_custom_fields(), update=True)
