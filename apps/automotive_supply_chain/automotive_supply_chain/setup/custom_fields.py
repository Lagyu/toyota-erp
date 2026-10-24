"""Custom fields automotive_supply_chain adds to standard and manufacturing DocTypes."""

from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def _f(fieldname, label, fieldtype, insert_after, **extra):
	return {
		"fieldname": fieldname,
		"label": label,
		"fieldtype": fieldtype,
		"insert_after": insert_after,
		**extra,
	}


def supply_chain_custom_fields() -> dict[str, list[dict]]:
	return {
		"Purchase Order": [
			_f(
				"automotive_release_type",
				"Release Type",
				"Select",
				"supplier",
				options="Standard\nBlanket Call-Off",
				default="Standard",
				in_standard_filter=1,
				description="Call-offs against an approved blanket order do not need a new approval.",
			),
			_f(
				"automotive_ack_status",
				"Supplier Acknowledgement",
				"Select",
				"automotive_release_type",
				options="Not Requested\nAwaiting\nAccepted\nAccepted With Changes\nRejected",
				default="Not Requested",
				read_only=1,
				allow_on_submit=1,
				in_standard_filter=1,
				no_copy=1,
			),
			_f(
				"automotive_ack_at",
				"Acknowledged At",
				"Datetime",
				"automotive_ack_status",
				read_only=1,
				allow_on_submit=1,
				no_copy=1,
			),
			_f(
				"automotive_supplier_schedule",
				"Supplier Schedule",
				"Link",
				"automotive_ack_at",
				options="Supplier Delivery Schedule",
				read_only=1,
				no_copy=1,
			),
		],
		"Purchase Order Item": [
			_f(
				"automotive_promised_date",
				"Supplier Promised Date",
				"Date",
				"schedule_date",
				read_only=1,
				allow_on_submit=1,
				no_copy=1,
			),
		],
		"Purchase Receipt": [
			_f(
				"automotive_asn",
				"Advance Shipping Notice",
				"Link",
				"supplier",
				options="Advance Shipping Notice",
				read_only=1,
				no_copy=1,
				search_index=1,
			),
		],
		"Purchase Receipt Item": [
			_f("automotive_supplier_lot", "Supplier Lot", "Data", "batch_no"),
			_f("automotive_asn_item", "ASN Item", "Data", "automotive_supplier_lot", read_only=1, no_copy=1),
		],
		"Purchase Invoice": [
			_f(
				"automotive_match_status",
				"Three-Way Match",
				"Select",
				"supplier",
				options="Not Checked\nMatched\nExceptions Open\nExceptions Accepted",
				default="Not Checked",
				read_only=1,
				allow_on_submit=1,
				in_standard_filter=1,
				no_copy=1,
			),
		],
		"Sales Order": [
			_f(
				"automotive_dealer_order",
				"Dealer Order",
				"Link",
				"customer",
				options="Dealer Order",
				read_only=1,
				no_copy=1,
				search_index=1,
			),
			_f(
				"automotive_max_discount_percent",
				"Max Line Discount %",
				"Percent",
				"automotive_dealer_order",
				read_only=1,
				description="Drives the discount approval policy.",
			),
			_f(
				"automotive_promised_date",
				"Promised Date",
				"Date",
				"automotive_max_discount_percent",
				read_only=1,
				allow_on_submit=1,
				no_copy=1,
			),
		],
		"Sales Order Item": [
			_f(
				"automotive_configuration",
				"Vehicle Configuration",
				"Link",
				"item_code",
				options="Vehicle Configuration",
			),
			_f(
				"automotive_allocated_qty",
				"Allocated Vehicles",
				"Float",
				"automotive_configuration",
				read_only=1,
				allow_on_submit=1,
				no_copy=1,
			),
		],
		"Delivery Note": [
			_f(
				"automotive_shipment_manifest",
				"Shipment Manifest",
				"Link",
				"customer",
				options="Shipment Manifest",
				read_only=1,
				allow_on_submit=1,
				no_copy=1,
			),
			_f(
				"automotive_acceptance_status",
				"Dealer Acceptance",
				"Select",
				"automotive_shipment_manifest",
				options="Pending\nAccepted\nAccepted With Damage\nRejected",
				default="Pending",
				read_only=1,
				allow_on_submit=1,
				no_copy=1,
			),
		],
		"Customer": [
			_f(
				"automotive_dealer_tier",
				"Dealer Tier",
				"Select",
				"customer_group",
				options="\nA\nB\nC",
				description="Allocation priority for Dealer Priority rules (A first).",
			),
			_f(
				"automotive_dealer_registration",
				"Dealer Registration",
				"Link",
				"automotive_dealer_tier",
				options="Dealer Registration",
				read_only=1,
			),
		],
		"Supplier": [
			_f(
				"automotive_onboarding",
				"Supplier Onboarding",
				"Link",
				"supplier_group",
				options="Supplier Onboarding",
				read_only=1,
			),
			_f(
				"automotive_risk_rating",
				"Risk Rating",
				"Select",
				"automotive_onboarding",
				options="\nLow\nMedium\nHigh",
			),
		],
		"Vehicle Unit": [
			_f(
				"automotive_allocation",
				"Allocation",
				"Link",
				"dealer",
				options="Vehicle Allocation",
				read_only=1,
			),
		],
	}


def apply() -> None:
	create_custom_fields(supply_chain_custom_fields(), update=True)
