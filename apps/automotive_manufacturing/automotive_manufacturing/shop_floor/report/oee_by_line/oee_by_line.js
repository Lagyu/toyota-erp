// Copyright (c) 2026, Toyota ERP Clone contributors
// License: GNU General Public License v3. See license.txt

frappe.query_reports["OEE by Line"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company" },
		{ fieldname: "plant", label: __("Plant"), fieldtype: "Link", options: "Plant" },
		{ fieldname: "production_line", label: __("Production Line"), fieldtype: "Link", options: "Production Line" },
		{
			fieldname: "from_date",
			label: __("From"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -6),
		},
		{ fieldname: "to_date", label: __("To"), fieldtype: "Date", default: frappe.datetime.get_today() },
	],
};
