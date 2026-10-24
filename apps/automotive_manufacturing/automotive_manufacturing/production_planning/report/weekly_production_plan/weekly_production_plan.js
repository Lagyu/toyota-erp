// Copyright (c) 2026, Toyota ERP Clone contributors
// License: GNU General Public License v3. See license.txt

frappe.query_reports["Weekly Production Plan"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company" },
		{ fieldname: "plant", label: __("Plant"), fieldtype: "Link", options: "Plant" },
		{ fieldname: "from_date", label: __("From Week"), fieldtype: "Date", default: frappe.datetime.get_today() },
		{ fieldname: "weeks", label: __("Weeks"), fieldtype: "Int", default: 8 },
	],
	formatter(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);
		// a flagged week stands out; the text says why
		if (column.fieldname === "status" && data && data.load_percent !== undefined && data.status
				&& (data.planned_qty > data.capacity || data.planned_qty < data.firm_orders)) {
			value = `<span class="text-danger">${value}</span>`;
		}
		return value;
	},
};
