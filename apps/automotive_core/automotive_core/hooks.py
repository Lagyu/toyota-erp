app_name = "automotive_core"
app_title = "Automotive Core"
app_publisher = "Toyota ERP Clone contributors"
app_description = "Enterprise structure, governance (approvals, delegation, audit), integration hub, finance controls and KPIs for Toyota ERP Clone"
app_email = "erp-team@hinode.example"
app_license = "GPL-3.0-only"

required_apps = ["frappe", "erpnext"]

# Installation / migration ------------------------------------------------------------
after_install = "automotive_core.setup.install.after_install"
after_migrate = "automotive_core.setup.install.after_migrate"
before_uninstall = "automotive_core.setup.install.before_uninstall"

# Desk assets ---------------------------------------------------------------------------
app_include_js = ["automotive_core.bundle.js"]
app_include_css = ["automotive_core.bundle.css"]

# Governed documents --------------------------------------------------------------------
# Other apps extend these lists through their own hooks (frappe.get_hooks merges them).
#
# automotive_cancel_reason_doctypes: cancelling requires a recorded reason
#   (automotive_core.governance.cancellation.cancel_with_reason).
# automotive_audited_doctypes: submit/cancel/amend are written to the Audit Event chain.
automotive_cancel_reason_doctypes = [
	"Journal Entry",
	"Payment Entry",
	"Purchase Invoice",
	"Sales Invoice",
	"Stock Entry",
	"Quality Inspection",
	"Stock Reconciliation",
	"Approval Delegation",
	"Period Close Checklist",
	"User Provisioning Request",
]
automotive_audited_doctypes = [
	"Journal Entry",
	"Payment Entry",
	"Purchase Invoice",
	"Sales Invoice",
	"Purchase Order",
	"Sales Order",
	"Stock Entry",
	"Stock Reconciliation",
	"Quality Inspection",
	"BOM",
	"Work Order",
	"Approval Delegation",
	"Period Close Checklist",
	"User Provisioning Request",
	"Bank Account",
]

# Plant scoping (ERP-01 / ERP-10): DocTypes that carry a Plant link which is derived
# when empty and enforced via User Permissions (see automotive_core.access.plant_scope).
automotive_plant_scoped_doctypes = [
	"Work Order",
	"Job Card",
	"Production Plan",
	"Material Request",
	"Purchase Order",
	"Purchase Receipt",
	"Stock Entry",
	"Quality Inspection",
	"Downtime Entry",
	"Pick List",
	"Delivery Note",
]

# Inbound message handlers by message type; other apps register theirs.
automotive_inbound_handlers = {
	"bank_statement": "automotive_core.finance_controls.bank_statements.bank_statement",
}

# KPI catalogue entries (ERP-12); each app lists the KPIs it computes.
automotive_kpi_definitions = ["automotive_core.analytics.kpis.DEFINITIONS"]

boot_session = "automotive_core.boot.boot_session"

# File: unreadable PDF content is refused like any mismatch (automotive_core.files.policy)
override_doctype_class = {"File": "automotive_core.files.policy.AutomotiveFile"}
# report downloads are recorded in the Access Log like the other exports (SEC-4)
override_whitelisted_methods = {
	"frappe.desk.query_report.export_query": "automotive_core.governance.exports.export_query",
}

doc_events = {
	"*": {
		"before_submit": "automotive_core.governance.approvals.enforce_on_submit",
		"on_submit": "automotive_core.governance.audit.on_submit",
		"before_cancel": "automotive_core.governance.cancellation.guard_cancel",
		"on_cancel": "automotive_core.governance.audit.on_cancel",
		"validate": "automotive_core.access.plant_scope.ensure_plant",
	},
	"Employee": {
		"validate": "automotive_core.access.user_permissions.employee_scope_is_provisioned",
	},
	"User": {
		"validate": "automotive_core.governance.sod.validate_user_roles",
		"on_update": [
			"automotive_core.governance.sod.record_warnings",
			"automotive_core.automotive_setup.access_sync.strip_ungranted_roles",
		],
	},
	"File": {
		"validate": "automotive_core.files.policy.validate_file",
		"after_insert": "automotive_core.files.policy.after_insert",
	},
	"Bank Account": {
		"validate": "automotive_core.finance_controls.bank_accounts.validate_bank_account",
	},
	"Payment Entry": {
		"validate": "automotive_core.finance_controls.bank_accounts.validate_payment_entry",
	},
	"Supplier": {
		"validate": "automotive_core.access.master_data.validate_supplier",
		"on_update": "automotive_core.access.master_data.record_accepted_similarity",
	},
	"Customer": {
		"validate": "automotive_core.access.master_data.validate_customer",
		"on_update": "automotive_core.access.master_data.record_accepted_similarity",
	},
	"Item": {
		"validate": "automotive_core.access.master_data.validate_item",
		"on_update": "automotive_core.access.master_data.record_accepted_similarity",
	},
}

has_permission = {
	"File": "automotive_core.files.policy.has_permission",
}

scheduler_events = {
	"cron": {
		# every minute: outbox dispatch and inbound retries
		"* * * * *": [
			"automotive_core.integration_hub.outbox.dispatch_due",
			"automotive_core.integration_hub.inbound.retry_due",
			"automotive_core.integration_hub.partners.poll_file_drops",
		],
		# every 5 minutes: seal audit events into the hash chain; record notification e-mail results
		"*/5 * * * *": [
			"automotive_core.governance.audit.seal_pending",
			"automotive_core.governance.notifications.sync_email_results",
		],
	},
	"hourly": [
		# files without a scan result (uploaded before scanning, or while clamd was down)
		"automotive_core.files.policy.rescan_pending",
		"automotive_core.governance.delegation.refresh_statuses",
		"automotive_core.governance.approvals.escalate_overdue",
		"automotive_core.automotive_setup.access_sync.refresh_assignments",
	],
	"daily": [
		"automotive_core.governance.audit.verify_chain_job",
		"automotive_core.governance.provisioning.expire_accounts",
		"automotive_core.integration_hub.maintenance.prune",
		"automotive_core.files.policy.apply_retention",
		"automotive_core.analytics.kpi.refresh_daily",
	],
}

before_request = [
	"automotive_core.observability.context.before_request",
	"automotive_core.observability.timing.before_request",
	# after the timing: a refused request is still counted and timed
	"automotive_core.access.request_budgets.before_request",
]
after_request = [
	"automotive_core.observability.context.after_request",
	"automotive_core.observability.timing.after_request",
]
# request latency histogram for the metrics endpoint; other apps add their work queues
automotive_metrics = ["automotive_core.observability.timing.metrics"]
before_job = ["automotive_core.observability.context.before_job"]

# Automated tests -----------------------------------------------------------------------
before_tests = "automotive_core.tests.fixtures.before_tests"

export_python_type_annotations = True
