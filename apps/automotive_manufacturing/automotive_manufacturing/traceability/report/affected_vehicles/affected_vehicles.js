// Copyright (c) 2026, Toyota ERP Clone contributors
// License: GNU General Public License v3. See license.txt

frappe.query_reports["Affected Vehicles"] = {
	filters: [
		{ fieldname: "item_code", label: __("Component Item"), fieldtype: "Link", options: "Item" },
		{
			fieldname: "batch_nos",
			label: __("Lots / Batches"),
			fieldtype: "Small Text",
			description: __("One per line or comma separated"),
		},
		{ fieldname: "serial_nos", label: __("Component Serials"), fieldtype: "Small Text" },
		{ fieldname: "supplier", label: __("Supplier"), fieldtype: "Link", options: "Supplier" },
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company" },
	],
};
