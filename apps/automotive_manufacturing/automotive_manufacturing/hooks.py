app_name = "automotive_manufacturing"
app_title = "Automotive Manufacturing"
app_publisher = "Toyota ERP Clone contributors"
app_description = "Product engineering and change control, planning and scheduling, shop-floor execution (Kanban, Andon, jidoka), quality and vehicle traceability"
app_email = "erp-team@hinode.example"
app_license = "GPL-3.0-only"

required_apps = ["frappe", "erpnext", "automotive_core"]

after_install = "automotive_manufacturing.setup.install.after_migrate"
after_migrate = "automotive_manufacturing.setup.install.after_migrate"

app_include_js = ["automotive_manufacturing.bundle.js"]

# Print formats: the Kanban Card prints its id as a QR code
jinja = {"methods": ["automotive_manufacturing.printing.automotive_qr_code"]}

# Extensions of automotive_core's governance lists ------------------------------------
automotive_cancel_reason_doctypes = ["Engineering Change Order", "Quality Disposition", "Shift Handover"]
automotive_audited_doctypes = [
	"Engineering Change Order",
	"Engineering Change Request",
	"Quality Disposition",
	"Planning Parameter Set",
	"Shift Handover",
]
automotive_tenant_extensions = ["automotive_manufacturing.setup.tenant.apply"]
automotive_inbound_handlers = {
	"production_event": "automotive_manufacturing.mes.handlers.production_event",
}
automotive_kpi_definitions = ["automotive_manufacturing.analytics.kpis.DEFINITIONS"]

doc_events = {
	"BOM": {
		"before_validate": "automotive_manufacturing.product_engineering.bom_revisions.before_validate",
		"validate": "automotive_manufacturing.product_engineering.bom_revisions.validate",
		"on_submit": "automotive_manufacturing.product_engineering.bom_revisions.on_submit",
		"on_update_after_submit": "automotive_manufacturing.product_engineering.bom_revisions.on_update_after_submit",
		"before_cancel": "automotive_manufacturing.product_engineering.bom_revisions.before_cancel",
	},
	"BOM Update Log": {
		"before_insert": "automotive_manufacturing.product_engineering.bom_revisions.guard_bom_update_tool",
	},
	"Work Order": {
		"before_validate": "automotive_manufacturing.product_engineering.bom_revisions.select_effective_bom",
		"validate": [
			"automotive_manufacturing.product_engineering.bom_revisions.validate_work_order_bom",
			"automotive_manufacturing.quality_control.holds.validate_work_order",
		],
		"on_submit": "automotive_manufacturing.traceability.vehicle_units.on_work_order_submit",
		"on_cancel": "automotive_manufacturing.traceability.vehicle_units.on_work_order_cancel",
	},
	"Stock Entry": {
		"validate": "automotive_manufacturing.quality_control.holds.validate_stock_entry",
		"on_submit": [
			"automotive_manufacturing.traceability.genealogy.on_stock_entry_submit",
			"automotive_manufacturing.traceability.vehicle_units.on_stock_entry_submit",
			"automotive_manufacturing.shop_floor.kanban.on_stock_entry_submit",
		],
		"on_cancel": [
			"automotive_manufacturing.traceability.genealogy.on_stock_entry_cancel",
			"automotive_manufacturing.traceability.vehicle_units.on_stock_entry_cancel",
			"automotive_manufacturing.shop_floor.kanban.on_stock_entry_cancel",
		],
	},
	"Job Card": {
		"validate": "automotive_manufacturing.quality_control.holds.validate_job_card",
	},
	"Quality Inspection": {
		"on_submit": "automotive_manufacturing.quality_control.inspections.on_submit",
		"on_cancel": "automotive_manufacturing.quality_control.inspections.on_cancel",
	},
	"Non Conformance": {
		"validate": "automotive_manufacturing.quality_control.nonconformance.validate",
	},
	"Purchase Receipt": {
		"validate": "automotive_manufacturing.quality_control.holds.validate_purchase_receipt",
		"on_submit": [
			"automotive_manufacturing.traceability.genealogy.stamp_supplier_lots",
			"automotive_manufacturing.shop_floor.kanban.on_purchase_receipt_submit",
		],
		"on_cancel": "automotive_manufacturing.shop_floor.kanban.on_purchase_receipt_cancel",
	},
	"Delivery Note": {
		"validate": "automotive_manufacturing.quality_control.holds.validate_outbound",
	},
	"Pick List": {
		"validate": "automotive_manufacturing.quality_control.holds.validate_outbound",
	},
	"Sales Invoice": {
		"validate": "automotive_manufacturing.quality_control.holds.validate_outbound",
	},
}

scheduler_events = {
	"cron": {
		"*/5 * * * *": ["automotive_manufacturing.shop_floor.kanban.escalate_overdue"],
	},
	"hourly": ["automotive_manufacturing.production_planning.planning_run.recover_stalled"],
	"daily": ["automotive_manufacturing.product_engineering.bom_revisions.apply_effectivity"],
}

# Every app prepares the test site the same way (Frappe runs the tested app's hook only).
before_tests = "automotive_core.tests.fixtures.before_tests"

# Work-queue gauges exposed by automotive_core.ops.health.metrics
automotive_metrics = ["automotive_manufacturing.ops_metrics.metrics"]
