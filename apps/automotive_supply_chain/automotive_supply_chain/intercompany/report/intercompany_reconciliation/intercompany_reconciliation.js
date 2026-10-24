// Copyright (c) 2026, Toyota ERP Clone contributors
// License: GNU General Public License v3. See license.txt

frappe.query_reports["Intercompany Reconciliation"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company" },
		{ fieldname: "to_date", label: __("As Of"), fieldtype: "Date", default: frappe.datetime.get_today() },
	],
	tree: true,
	initial_depth: 1,
};
