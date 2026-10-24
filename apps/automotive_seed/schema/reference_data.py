"""Reference Data: seed runs for non-production sites (requirements §5)."""

from dsl import (
	DocType,
	check,
	code,
	column,
	data,
	date,
	datetime,
	int_,
	perm,
	section,
	select,
	small_text,
	table,
	text,
)
from roles import AUDITOR, IT_OPERATOR, SYSTEM

DOCTYPES = [
	DocType(
		"Seed Run",
		"Reference Data",
		autoname="format:SEED-{profile}-{seed}",
		title_field="profile",
		icon="sprout",
		description="One deterministic synthetic-data generation (profile + seed); resumable, with validation results.",
		fields=[
			select("profile", "Profile", ["small", "standard", "large"], reqd=1, in_list_view=1),
			int_("seed", "Seed", reqd=1, in_list_view=1),
			select("status", "Status", ["Queued", "Running", "Paused", "Completed", "Failed", "Validated",
				"Validation Failed"], default="Queued", read_only=1, in_list_view=1, in_standard_filter=1),
			data("stage", "Current Stage", read_only=1, in_list_view=1),
			date("simulated_date", "Simulated Date", read_only=1, in_list_view=1),
			column(),
			date("start_date", "History Start", read_only=1),
			date("end_date", "History End", read_only=1),
			datetime("started_at", "Started At", read_only=1),
			datetime("finished_at", "Finished At", read_only=1),
			check("stop_requested", "Stop Requested", read_only=1),
			section("Stages"),
			table("stages", "Stages", "Seed Run Stage", read_only=1),
			section("Counts"),
			code("counts", "Document Counts", options="JSON", read_only=1),
			section("Validation"),
			code("validation", "Invariant Results", options="JSON", read_only=1),
			text("error", "Error", read_only=1),
		],
		permissions=[perm(SYSTEM, "rw pe"), perm(IT_OPERATOR, "r pe"), perm(AUDITOR, "r pe")],
	),
	DocType(
		"Seed Run Stage",
		"Reference Data",
		child=True,
		fields=[
			data("stage", "Stage", in_list_view=1, read_only=1),
			select("stage_status", "Status", ["Pending", "Done"], default="Pending", in_list_view=1, read_only=1),
			datetime("completed_at", "Completed At", in_list_view=1, read_only=1),
			int_("documents", "Documents", in_list_view=1, read_only=1),
			small_text("note", "Note", read_only=1),
		],
	),
]
