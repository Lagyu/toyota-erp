// Copyright (c) 2026, Toyota ERP Clone contributors
// License: GNU General Public License v3. See license.txt

frappe.query_reports["Vehicle Genealogy"] = {
	filters: [
		{
			fieldname: "serial_no",
			label: __("VIN / Serial No"),
			fieldtype: "Link",
			options: "Serial No",
			reqd: 1,
		},
	],
};
