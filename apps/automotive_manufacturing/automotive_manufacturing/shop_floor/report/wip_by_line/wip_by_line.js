// Copyright (c) 2026, Toyota ERP Clone contributors
// License: GNU General Public License v3. See license.txt

frappe.query_reports["WIP by Line"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company" },
		{ fieldname: "plant", label: __("Plant"), fieldtype: "Link", options: "Plant" },
		{ fieldname: "production_line", label: __("Production Line"), fieldtype: "Link", options: "Production Line" },
		{ fieldname: "show_orders", label: __("Show Work Orders"), fieldtype: "Check" },
	],
	tree: true,
	initial_depth: 1,
};
