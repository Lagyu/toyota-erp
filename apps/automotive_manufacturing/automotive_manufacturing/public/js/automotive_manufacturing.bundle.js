// Desk actions for manufacturing documents.
frappe.provide("automotive.manufacturing");

const call = (method, args) => frappe.call({ method, args, freeze: true }).then((r) => r.message);

frappe.ui.form.on("Engineering Change Request", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__("Assess Impact"), () =>
				call("automotive_manufacturing.product_engineering.change_control.assess_impact", { name: frm.docname }).then(() =>
					frm.reload_doc()
				)
			);
		}
		if (frm.doc.docstatus === 1 && ["Under Review", "Approved"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Create ECO"), () =>
				call("automotive_manufacturing.product_engineering.change_control.make_eco", { ecr: frm.docname }).then((eco) =>
					frappe.set_route("Form", "Engineering Change Order", eco)
				)
			);
		}
	},
});

frappe.ui.form.on("Planning Run", {
	refresh(frm) {
		const m = "automotive_manufacturing.production_planning.planning_run.";
		if (frm.doc.status === "Completed" && !frm.doc.replay_of) {
			frm.add_custom_button(__("Approve All"), () => call(m + "decide_all", { name: frm.docname, decision: "Approved" }).then(() => frm.reload_doc()), __("Proposals"));
			frm.add_custom_button(__("Release"), () => call(m + "release", { name: frm.docname }).then(() => frm.reload_doc()), __("Proposals"));
			frm.add_custom_button(__("Replay & Verify"), () => call(m + "replay", { name: frm.docname }).then((n) => frappe.set_route("Form", "Planning Run", n)));
		}
		if (["Queued", "Running"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Request Cancel"), () => call(m + "request_cancel", { name: frm.docname }).then(() => frm.reload_doc()));
			setTimeout(() => frm.reload_doc(), 5000);
		}
	},
});

frappe.ui.form.on("Andon Event", {
	refresh(frm) {
		if (frm.is_new()) return;
		const m = "automotive_manufacturing.shop_floor.andon.";
		const s = frm.doc.status;
		frm.page.wrapper.addClass("automotive-touch");
		if (s === "Raised") frm.add_custom_button(__("Acknowledge"), () => call(m + "acknowledge", { name: frm.docname }).then(() => frm.reload_doc()));
		if (["Raised", "Acknowledged"].includes(s))
			frm.add_custom_button(__("Record Containment"), () =>
				frappe.prompt({ fieldname: "action", fieldtype: "Small Text", label: __("Containment action"), reqd: 1 }, (v) =>
					call(m + "contain", { name: frm.docname, action: v.action }).then(() => frm.reload_doc())
				)
			);
		if (["Acknowledged", "Contained"].includes(s))
			frm.add_custom_button(__("Authorise Restart"), () =>
				frappe.prompt({ fieldname: "note", fieldtype: "Small Text", label: __("Note") }, (v) =>
					call(m + "authorize_restart", { name: frm.docname, note: v.note }).then(() => frm.reload_doc())
				)
			);
		if (s === "Restarted" || (frm.doc.stop_type === "Call" && s !== "Closed" && s !== "Raised"))
			frm.add_custom_button(__("Close"), () => call(m + "close", { name: frm.docname }).then(() => frm.reload_doc()));
	},
});

frappe.ui.form.on("Quality Hold", {
	refresh(frm) {
		if (frm.doc.status === "Active" && !frm.is_new()) {
			frm.add_custom_button(__("Release Hold"), () =>
				frappe.prompt({ fieldname: "note", fieldtype: "Small Text", label: __("Release note"), reqd: 1 }, (v) =>
					call("automotive_manufacturing.quality_control.holds.release", { name: frm.docname, note: v.note }).then(() =>
						frm.reload_doc()
					)
				)
			);
		}
	},
});

frappe.ui.form.on("Recall Campaign", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__("Analyze Affected Units"), () =>
				call("automotive_manufacturing.traceability.recall.run_analysis", { name: frm.docname }).then(() => frm.reload_doc())
			);
		}
	},
});

frappe.ui.form.on("Kanban Card", {
	refresh(frm) {
		if (frm.doc.status === "Full") {
			frm.add_custom_button(__("Signal (Card Emptied)"), () =>
				call("automotive_manufacturing.shop_floor.kanban.scan_card", { card_id: frm.docname }).then(() => frm.reload_doc())
			);
		}
	},
});

frappe.ui.form.on("Shift Handover", {
	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.status === "Handed Over" && frm.doc.incoming_supervisor === frappe.session.user) {
			frm.add_custom_button(__("Acknowledge Handover"), () =>
				call("automotive_manufacturing.shop_floor.handover.acknowledge", { name: frm.docname }).then(() => frm.reload_doc())
			);
		}
	},
});

frappe.ui.form.on("Plant Schedule", {
	refresh(frm) {
		const m = "automotive_manufacturing.production_planning.schedule.";
		if (frm.doc.status === "Draft" && !frm.is_new()) {
			frm.add_custom_button(__("Release"), () => call(m + "set_status", { name: frm.docname, status: "Released" }).then(() => frm.reload_doc()));
		}
		if (["Draft", "Released"].includes(frm.doc.status) && !frm.is_new()) {
			frm.add_custom_button(__("Rebuild Sequence"), () =>
				frappe.prompt({ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: frm.doc.status === "Released" ? 1 : 0 }, (v) =>
					call(m + "build", { production_line: frm.doc.production_line, schedule_date: frm.doc.schedule_date, reason: v.reason }).then(() =>
						frm.reload_doc()
					)
				)
			);
		}
	},
});
