// Copyright (c) 2026, Toyota ERP Clone contributors
// License: GNU General Public License v3. See license.txt

frappe.query_reports["Group Consolidation"] = {
	filters: [
		{ fieldname: "from_date", label: __("Period From"), fieldtype: "Date" },
		{ fieldname: "to_date", label: __("As Of"), fieldtype: "Date", default: frappe.datetime.get_today() },
		{
			fieldname: "presentation_currency",
			label: __("Presentation Currency"),
			fieldtype: "Link",
			options: "Currency",
			default: "JPY",
		},
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company" },
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		return data && data.bold ? `<b>${value}</b>` : value;
	},
};
