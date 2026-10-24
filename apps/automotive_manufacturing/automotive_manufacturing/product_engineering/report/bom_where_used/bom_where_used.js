// Copyright (c) 2026, Toyota ERP Clone contributors
// License: GNU General Public License v3. See license.txt

frappe.query_reports["BOM Where Used"] = {
	filters: [
		{ fieldname: "item_code", label: __("Item"), fieldtype: "Link", options: "Item", reqd: 1 },
		{ fieldname: "include_drafts", label: __("Include Draft BOMs"), fieldtype: "Check" },
	],
	tree: true,
	initial_depth: 3,
};
