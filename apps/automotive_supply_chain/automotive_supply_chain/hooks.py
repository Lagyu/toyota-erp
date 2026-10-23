app_name = "automotive_supply_chain"
app_title = "Automotive Supply Chain"
app_publisher = "Toyota ERP Clone contributors"
app_description = "Supplier collaboration, procurement controls, logistics and warehousing, dealer operations and intercompany flows"
app_email = "erp-team@hinode.example"
app_license = "GPL-3.0-only"

required_apps = ["frappe", "erpnext", "automotive_core", "automotive_manufacturing"]

after_install = "automotive_supply_chain.setup.install.after_migrate"
after_migrate = "automotive_supply_chain.setup.install.after_migrate"

app_include_js = ["automotive_supply_chain.bundle.js"]

# Extensions of automotive_core governance -------------------------------------------
automotive_cancel_reason_doctypes = ["Sales Order", "Purchase Order", "Delivery Note", "Purchase Receipt",
	"Shipment Manifest", "Advance Shipping Notice", "Sourcing Decision", "Transfer Price Agreement"]
automotive_audited_doctypes = ["Supplier Onboarding", "Sourcing Decision", "Supplier Delivery Schedule",
	"Advance Shipping Notice", "Purchase Receipt", "Delivery Note", "Shipment Manifest", "Dealer Registration",
	"Dealer Order", "Dealer Order Change", "Delivery Acceptance", "Transfer Price Agreement", "Cycle Count"]
automotive_tenant_extensions = ["automotive_supply_chain.setup.tenant.apply"]
automotive_planning_supplier_resolvers = ["automotive_supply_chain.supplier_collaboration.sourcing.preferred_supplier"]
automotive_inbound_handlers = {
	"asn": "automotive_supply_chain.integrations.handlers.asn",
	"po_acknowledgement": "automotive_supply_chain.integrations.handlers.po_acknowledgement",
	"dealer_order": "automotive_supply_chain.integrations.handlers.dealer_order",
	"dealer_order_change": "automotive_supply_chain.integrations.handlers.dealer_order_change",
}
automotive_kpi_definitions = ["automotive_supply_chain.analytics.kpis.DEFINITIONS"]

doc_events = {
	"Purchase Order": {
		"validate": [
			"automotive_supply_chain.supplier_collaboration.sourcing.validate_purchase_order_sources",
			"automotive_supply_chain.intercompany.transfer_pricing.validate_purchase_order",
		],
		"on_submit": "automotive_supply_chain.supplier_collaboration.schedules.on_purchase_order_submit",
	},
	"Purchase Receipt": {
		"on_submit": "automotive_supply_chain.supplier_collaboration.shipments.on_purchase_receipt_submit",
		"on_cancel": "automotive_supply_chain.supplier_collaboration.shipments.on_purchase_receipt_cancel",
	},
	"Purchase Invoice": {
		"after_insert": "automotive_supply_chain.procurement_controls.three_way_match.after_insert_purchase_invoice",
		"validate": "automotive_supply_chain.procurement_controls.three_way_match.validate_purchase_invoice",
		"before_submit": "automotive_supply_chain.procurement_controls.three_way_match.before_submit_purchase_invoice",
	},
	"Sales Order": {
		"validate": [
			"automotive_supply_chain.dealer_operations.orders.validate_sales_order",
			"automotive_supply_chain.intercompany.transfer_pricing.validate_sales_order",
		],
		"on_submit": "automotive_supply_chain.dealer_operations.allocation.on_sales_order_submit",
		"on_cancel": "automotive_supply_chain.dealer_operations.allocation.on_sales_order_cancel",
	},
	"Delivery Note": {
		"on_submit": "automotive_supply_chain.dealer_operations.fulfilment.on_delivery_note_submit",
		"on_cancel": "automotive_supply_chain.dealer_operations.fulfilment.on_delivery_note_cancel",
	},
	"Sales Invoice": {
		"on_submit": "automotive_supply_chain.dealer_operations.fulfilment.on_sales_invoice_submit",
		"on_cancel": "automotive_supply_chain.dealer_operations.fulfilment.on_sales_invoice_cancel",
	},
	"Payment Entry": {
		"on_submit": "automotive_supply_chain.dealer_operations.fulfilment.on_payment_entry_change",
		"on_cancel": "automotive_supply_chain.dealer_operations.fulfilment.on_payment_entry_change",
	},
}

scheduler_events = {
	"cron": {
		"*/5 * * * *": ["automotive_supply_chain.supplier_collaboration.schedules.release_kanban_calloffs"],
		"*/15 * * * *": ["automotive_supply_chain.dealer_operations.allocation.allocate_all"],
	},
}

# Every app prepares the test site the same way (Frappe runs the tested app's hook only).
before_tests = "automotive_core.tests.fixtures.before_tests"
