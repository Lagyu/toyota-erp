// Copyright (c) 2026, Toyota ERP Clone contributors
// License: GNU General Public License v3. See license.txt

frappe.query_reports["Inventory Position"] = {
	filters: [
		{ fieldname: "company", label: __("Company"), fieldtype: "Link", options: "Company" },
		{ fieldname: "plant", label: __("Plant"), fieldtype: "Link", options: "Plant" },
		{ fieldname: "warehouse", label: __("Warehouse"), fieldtype: "Link", options: "Warehouse" },
		{ fieldname: "item_code", label: __("Item"), fieldtype: "Link", options: "Item" },
		{ fieldname: "item_group", label: __("Item Group"), fieldtype: "Link", options: "Item Group" },
		{ fieldname: "include_zero", label: __("Include Empty Bins"), fieldtype: "Check" },
	],
};
