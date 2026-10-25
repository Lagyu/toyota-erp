"""Report downloads in the Access Log (SEC-4).

Frappe records the exports of the report view and of the Data Export tool in the Access Log,
but not the download of a query or script report. Its download method is replaced by this one
(``override_whitelisted_methods``), which runs Frappe's own, export check included, and then
records the download the same way: user, report, its DocType, file type and filters.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.core.doctype.access_log.access_log import make_access_log
from frappe.desk import query_report


@frappe.whitelist()
def export_query():
	"""The desk's download of a query or script report: Frappe's
	`frappe.desk.query_report.export_query` (the path the desk calls), recorded in the Access Log.

	Needs the export right on the report's DocType; the parameters are Frappe's."""
	form = frappe._dict(frappe.local.form_dict)
	if not form.report_name or not frappe.db.exists("Report", form.report_name):
		frappe.throw(_("Report {0} not found").format(form.report_name), frappe.DoesNotExistError)
	result = query_report.export_query()
	make_access_log(
		doctype=frappe.get_cached_value("Report", form.report_name, "ref_doctype"),
		report_name=form.report_name,
		file_type=form.file_format_type,
		filters=form.filters,
	)
	return result
