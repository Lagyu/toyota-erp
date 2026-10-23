"""Product Engineering: vehicle models, option compatibility, configurations and
engineering change control (ERP-02)."""

from dsl import (
	DocType,
	amended_from,
	check,
	column,
	currency,
	data,
	date,
	datetime,
	float_,
	int_,
	link,
	naming_series,
	percent,
	perm,
	read_only,
	section,
	select,
	small_text,
	table,
	text,
	text_editor,
)
from roles import *  # noqa: F403

MODULE = "Product Engineering"
READ_ALL = [perm(r, "r p") for r in INTERNAL]  # noqa: F405
ENGINEERING = [
	perm(SYSTEM, "rwcd pe i"),  # noqa: F405
	perm(ENGINEER, "rwc pe i"),  # noqa: F405
	perm(ENGINEERING_MANAGER, "rwcd pe i"),  # noqa: F405
]

DOCTYPES = [
	DocType(
		"Vehicle Model",
		MODULE,
		autoname="field:model_code",
		title_field="model_name",
		show_title_field_in_link=True,
		icon="car",
		allow_import=True,
		description="A product family member (vehicle line or powertrain assembly) and its configurable template.",
		fields=[
			data("model_code", "Model Code", reqd=1, unique=1, in_list_view=1, set_only_once=1),
			data("model_name", "Model Name", reqd=1, in_list_view=1, translatable=1),
			select("product_family", "Product Family", [
				"Passenger Car", "Crossover", "Light Commercial", "Powertrain", "Electric Drive",
			], reqd=1, in_standard_filter=1),
			link("template_item", "Template Item", "Item", reqd=1,
				description="Item with variants; each buildable configuration is a variant of it."),
			select("lifecycle_status", "Lifecycle", ["Development", "Active", "Run-out", "Discontinued"],
				default="Active", in_list_view=1, in_standard_filter=1),
			column(),
			link("default_plant", "Build Plant", "Plant", reqd=1),
			link("production_line", "Production Line", "Production Line", reqd=1),
			date("start_of_production", "Start of Production"),
			date("end_of_production", "End of Production"),
			data("vin_prefix", "VIN Prefix (WMI + VDS)", length=8,
				description="8 characters before the check digit; synthetic, not an assigned WMI."),
			int_("warranty_months", "Warranty (Months)", default="36"),
			section("Configuration"),
			table("option_groups", "Option Groups", "Vehicle Model Option Group"),
			section("Description"),
			small_text("description", "Description"),
		],
		permissions=[*ENGINEERING, *READ_ALL, perm(DEALER_PORTAL, "r")],  # noqa: F405
	),
	DocType(
		"Vehicle Model Option Group",
		MODULE,
		child=True,
		fields=[
			link("attribute", "Attribute", "Item Attribute", reqd=1, in_list_view=1),
			check("required", "Required", default="1", in_list_view=1),
			int_("display_order", "Order", in_list_view=1),
			data("label", "Label", in_list_view=1, translatable=1),
		],
	),
	DocType(
		"Vehicle Option Rule",
		MODULE,
		autoname="field:rule_code",
		icon="list-filter",
		allow_import=True,
		description="Compatibility rule between configuration options (requires / excludes / allowed values).",
		fields=[
			data("rule_code", "Rule Code", reqd=1, unique=1, in_list_view=1),
			link("vehicle_model", "Vehicle Model", "Vehicle Model", reqd=1, in_list_view=1, in_standard_filter=1),
			select("rule_type", "Rule Type", ["Requires", "Excludes", "Allowed Values"], reqd=1, in_list_view=1),
			check("is_active", "Active", default="1"),
			column(),
			date("effective_from", "Effective From"),
			date("effective_to", "Effective To"),
			section("Condition", description="Leave empty for unconditional Allowed Values rules."),
			link("if_attribute", "If Attribute", "Item Attribute"),
			small_text("if_values", "Is One Of", description="One value per line."),
			section("Consequence"),
			link("then_attribute", "Then Attribute", "Item Attribute", reqd=1),
			small_text("then_values", "Values", reqd=1, description="One value per line."),
			data("message", "Message Shown To Users", translatable=1),
		],
		permissions=[*ENGINEERING, *READ_ALL, perm(DEALER_PORTAL, "r")],  # noqa: F405
	),
	DocType(
		"Vehicle Configuration",
		MODULE,
		autoname="naming_series:",
		title_field="configuration_key",
		search_fields="vehicle_model,variant_item,status",
		icon="sliders-horizontal",
		description="A validated set of option selections resolved to one buildable variant item.",
		fields=[
			naming_series("VCFG-.YYYY.-"),
			link("vehicle_model", "Vehicle Model", "Vehicle Model", reqd=1, in_list_view=1, in_standard_filter=1),
			data("configuration_key", "Configuration Key", read_only=1, search_index=1),
			link("variant_item", "Variant Item", "Item", read_only=1, in_list_view=1),
			column(),
			select("status", "Status", ["Draft", "Valid", "Invalid"], default="Draft", read_only=1, in_list_view=1,
				in_standard_filter=1),
			datetime("validated_at", "Validated At", read_only=1),
			small_text("violations", "Violations", read_only=1),
			section("Selections"),
			table("selections", "Selections", "Vehicle Configuration Selection", reqd=1),
		],
		permissions=[
			perm(SYSTEM, "rwcd pe"),  # noqa: F405
			perm(ENGINEER, "rwc pe"),  # noqa: F405
			perm(SALES_MANAGER, "rwc pe"),  # noqa: F405
			perm(DEALER_LIAISON, "rwc pe"),  # noqa: F405
			perm(DEALER_PORTAL, "rc"),  # noqa: F405
			*READ_ALL,
		],
	),
	DocType(
		"Vehicle Configuration Selection",
		MODULE,
		child=True,
		fields=[
			link("attribute", "Attribute", "Item Attribute", reqd=1, in_list_view=1),
			data("attribute_value", "Value", reqd=1, in_list_view=1),
		],
	),
	DocType(
		"Engineering Change Request",
		MODULE,
		autoname="naming_series:",
		submittable=True,
		title_field="title",
		icon="file-pen-line",
		description="Proposal to change a product; carries the impact assessment that feeds the ECO.",
		fields=[
			naming_series("ECR-.YYYY.-"),
			data("title", "Title", reqd=1, in_list_view=1),
			link("vehicle_model", "Vehicle Model", "Vehicle Model", in_standard_filter=1),
			select("change_reason", "Reason", [
				"Cost Reduction", "Quality Improvement", "Supplier Change", "Regulatory", "Customer Request",
				"Production Issue", "Field Issue",
			], reqd=1, in_standard_filter=1),
			select("priority", "Priority", ["Low", "Medium", "High", "Critical"], default="Medium", in_list_view=1),
			column(),
			select("status", "Status", ["Draft", "Under Review", "Approved", "Rejected", "Converted"], default="Draft",
				read_only=1, in_list_view=1, in_standard_filter=1, allow_on_submit=1, no_copy=1),
			link("requested_by", "Requested By", "User", read_only=1),
			link("nonconformance", "Related Nonconformance", "Non Conformance"),
			section("Description"),
			text_editor("description", "Description", reqd=1),
			section("Affected Items"),
			table("affected_items", "Affected Items", "ECR Affected Item", reqd=1),
			section("Impact Assessment"),
			datetime("impact_assessed_at", "Assessed At", read_only=1),
			int_("open_work_orders", "Open Work Orders", read_only=1),
			float_("on_hand_qty", "On Hand (Affected Items)", read_only=1),
			float_("open_purchase_qty", "Open Purchase Qty", read_only=1),
			column(),
			float_("wip_qty", "Open Production Qty", read_only=1),
			currency("estimated_cost_delta", "Estimated Cost Delta / Unit"),
			small_text("impact_summary", "Impact Summary", read_only=1),
			amended_from("Engineering Change Request"),
		],
		permissions=[
			perm(SYSTEM, "rwcd sxa pe"),  # noqa: F405
			perm(ENGINEER, "rwc sxa pe"),  # noqa: F405
			perm(ENGINEERING_MANAGER, "rwc sxa pe"),  # noqa: F405
			perm(QUALITY_MANAGER, "rwc s pe"),  # noqa: F405
			*READ_ALL,
		],
	),
	DocType(
		"ECR Affected Item",
		MODULE,
		child=True,
		fields=[
			link("item_code", "Item", "Item", reqd=1, in_list_view=1),
			link("current_bom", "Current BOM", "BOM", in_list_view=1),
			small_text("proposed_change", "Proposed Change", in_list_view=1),
		],
	),
	DocType(
		"Engineering Change Order",
		MODULE,
		autoname="naming_series:",
		submittable=True,
		title_field="title",
		icon="git-pull-request-arrow",
		description="Approved change: on submit, new BOM revisions are released with effectivity.",
		fields=[
			naming_series("ECO-.YYYY.-"),
			data("title", "Title", reqd=1, in_list_view=1),
			link("engineering_change_request", "Change Request", "Engineering Change Request"),
			link("vehicle_model", "Vehicle Model", "Vehicle Model", in_standard_filter=1),
			link("company", "Company", "Company", reqd=1),
			column(),
			date("effective_date", "Effective Date", reqd=1, in_list_view=1,
				description="Work Orders planned to start on or after this date use the new revisions."),
			select("stock_disposition", "Existing Stock", [
				"Use Existing Stock", "Rework", "Scrap", "Return to Supplier",
			], default="Use Existing Stock", reqd=1),
			select("implementation_status", "Implementation", ["Not Started", "Implemented", "Failed"],
				default="Not Started", read_only=1, in_list_view=1, allow_on_submit=1, no_copy=1),
			datetime("implemented_at", "Implemented At", read_only=1, allow_on_submit=1, no_copy=1),
			section("Changes"),
			table("changes", "Changes", "ECO Change", reqd=1),
			section("Released Revisions"),
			table("revisions", "Released Revisions", "ECO BOM Revision", read_only=1, allow_on_submit=1, no_copy=1),
			section("Notes"),
			small_text("implementation_notes", "Notes"),
			amended_from("Engineering Change Order"),
		],
		permissions=[
			perm(SYSTEM, "rwcd sxa pe"),  # noqa: F405
			perm(ENGINEER, "rwc s pe"),  # noqa: F405
			perm(ENGINEERING_MANAGER, "rwc sxa pe"),  # noqa: F405
			*READ_ALL,
		],
	),
	DocType(
		"ECO Change",
		MODULE,
		child=True,
		fields=[
			select("change_type", "Change", ["Add Component", "Remove Component", "Replace Component", "Change Quantity"],
				reqd=1, in_list_view=1),
			link("target_bom", "BOM", "BOM", reqd=1, in_list_view=1),
			link("component", "Component", "Item", in_list_view=1),
			link("new_component", "New Component", "Item", in_list_view=1),
			float_("qty", "Qty Per Parent", in_list_view=1, non_negative=1),
			percent("scrap_factor_percent", "Scrap Factor %"),
			small_text("reason", "Reason"),
		],
	),
	DocType(
		"ECO BOM Revision",
		MODULE,
		child=True,
		fields=[
			link("old_bom", "Superseded BOM", "BOM", in_list_view=1, read_only=1),
			link("new_bom", "New BOM", "BOM", in_list_view=1, read_only=1),
			data("revision", "Revision", in_list_view=1, read_only=1),
			select("reason", "Why", ["Direct Change", "Parent Of Changed Assembly"], in_list_view=1, read_only=1),
		],
	),
]
