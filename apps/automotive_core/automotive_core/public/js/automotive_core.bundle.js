// Desk extensions shared by every Hinode ERP app:
//  * approval panel on DocTypes governed by an Approval Policy
//  * "cancel with reason" for DocTypes whose cancellation must be justified
frappe.provide("automotive");

automotive.call = (method, args) =>
	frappe.call({ method, args, freeze: true }).then((r) => r.message);

automotive.approvals = {
	async render(frm) {
		if (frm.is_new() || frm.doc.docstatus !== 0) return;
		const status = await frappe
			.call({
				method: "automotive_core.governance.approvals.get_status",
				args: { doctype: frm.doctype, name: frm.docname },
			})
			.then((r) => r.message);
		if (!status || (!status.policy && !status.request)) return;
		const req = status.request;
		const group = __("Approval");
		if (!req || ["Rejected", "Withdrawn", "Invalidated"].includes(req.status)) {
			if (req) {
				frm.dashboard.set_headline_alert(
					__("Approval {0}: {1}", [__(req.status).toLowerCase(), req.name]),
					"red"
				);
			}
			if (status.policy) {
				frm.add_custom_button(
					__("Request Approval"),
					() => automotive.approvals.request(frm),
					group
				);
			}
			return;
		}
		const steps = req.steps
			.map((s) => `${s.step_no}. ${__(s.step_name)} — ${__(s.status)}${s.decided_by ? " (" + s.decided_by + ")" : ""}`)
			.join("<br>");
		if (req.status === "Approved") {
			const color = status.hash_matches ? "green" : "orange";
			const note = status.hash_matches
				? __("Approved ({0}); the document can be submitted.", [req.name])
				: __("Approved ({0}) but the document changed afterwards; request approval again.", [req.name]);
			frm.dashboard.set_headline_alert(`${note}<br><small>${steps}</small>`, color);
			if (!status.hash_matches) {
				frm.add_custom_button(__("Request Approval"), () => automotive.approvals.request(frm), group);
			}
			return;
		}
		frm.dashboard.set_headline_alert(
			`${__("Awaiting approval ({0}), step {1}", [req.name, req.current_step])}<br><small>${steps}</small>`,
			"orange"
		);
		if (status.can_decide) {
			frm.add_custom_button(__("Approve"), () => automotive.approvals.decide(frm, req.name, "Approve"), group);
			frm.add_custom_button(__("Reject"), () => automotive.approvals.decide(frm, req.name, "Reject"), group);
		}
		if (frappe.session.user === frm.doc.owner) {
			frm.add_custom_button(
				__("Withdraw Request"),
				() =>
					automotive
						.call("automotive_core.governance.approvals.withdraw", { request: req.name })
						.then(() => frm.reload_doc()),
				group
			);
		}
	},

	request(frm) {
		const go = () =>
			automotive
				.call("automotive_core.governance.approvals.request_approval", {
					doctype: frm.doctype,
					name: frm.docname,
				})
				.then(() => frm.reload_doc());
		if (frm.is_dirty()) {
			frm.save().then(go);
		} else {
			go();
		}
	},

	decide(frm, request, decision) {
		frappe.prompt(
			[
				{
					fieldname: "comment",
					fieldtype: "Small Text",
					label: __("Comment"),
					reqd: decision === "Reject" ? 1 : 0,
				},
			],
			(values) =>
				automotive
					.call("automotive_core.governance.approvals.decide", {
						request,
						decision,
						comment: values.comment,
					})
					.then(() => frm.reload_doc()),
			decision === "Approve" ? __("Approve {0}", [frm.docname]) : __("Reject {0}", [frm.docname]),
			decision === "Approve" ? __("Approve") : __("Reject")
		);
	},
};

automotive.cancel_with_reason = (frm) => {
	frappe.prompt(
		[
			{
				fieldname: "reason",
				fieldtype: "Small Text",
				label: __("Reason for cancellation"),
				reqd: 1,
				description: __("Recorded in the audit trail. At least 10 characters."),
			},
		],
		(values) =>
			automotive
				.call("automotive_core.governance.cancellation.cancel_with_reason", {
					doctype: frm.doctype,
					name: frm.docname,
					reason: values.reason,
				})
				.then(() => frm.reload_doc()),
		__("Cancel {0}", [frm.docname]),
		__("Cancel Document")
	);
};

$(document).on("app_ready", () => {
	const boot = frappe.boot.automotive || {};
	(boot.governed_doctypes || []).forEach((doctype) => {
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				automotive.approvals.render(frm);
			},
		});
	});
	(boot.cancel_reason_doctypes || []).forEach((doctype) => {
		frappe.ui.form.on(doctype, {
			refresh(frm) {
				if (frm.doc.docstatus === 1) {
					frm.savecancel = () => automotive.cancel_with_reason(frm);
				}
			},
		});
	});
});

// Period close (ERP-09): run the ledger checks, waive a failed check with a reason.
frappe.ui.form.on("Period Close Checklist", {
	refresh(frm) {
		if (frm.is_new() || frm.doc.docstatus !== 0) return;
		const m = "automotive_core.finance_controls.period_close.";
		frm.add_custom_button(__("Run Checks"), () =>
			automotive.call(m + "run_checks", { name: frm.docname }).then(() => frm.reload_doc())
		);
		const failed = (frm.doc.tasks || []).filter((t) => t.status === "Failed");
		if (failed.length) {
			frm.add_custom_button(__("Waive a Check"), () =>
				frappe.prompt(
					[
						{ fieldname: "check_code", fieldtype: "Select", label: __("Check"), reqd: 1,
							options: failed.map((t) => t.check_code).join("\n") },
						{ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
					],
					(v) => automotive.call(m + "waive", { name: frm.docname, ...v }).then(() => frm.reload_doc()),
					__("Waive a failed check"),
					__("Waive")
				)
			);
		}
	},
});

// Integration hub (ERP-11): operators replay a failed inbound message once its cause is
// fixed, and retry an outbound delivery; both keep the exactly-once guarantees.
frappe.ui.form.on("Integration Inbound Message", {
	refresh(frm) {
		if (!["Failed", "Dead Letter", "Rejected"].includes(frm.doc.status)) return;
		if (!frappe.user.has_role(["IT Operator", "System Manager"])) return;
		frm.add_custom_button(__("Replay"), () =>
			automotive
				.call("automotive_core.integration_hub.inbound.replay", { name: frm.docname })
				.then(() => frm.reload_doc())
		);
	},
});

// Inbound secret rotation: the new secret is shown once; the current one stays valid for the
// overlap so the partner can switch without refused deliveries.
frappe.ui.form.on("Integration Partner", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role(["IT Operator", "System Manager"])) return;
		frm.add_custom_button(__("Rotate Inbound Secret"), () =>
			frappe.prompt(
				{ fieldname: "overlap_hours", fieldtype: "Int", label: __("Keep the current secret valid for (hours)"), default: 24, reqd: 1 },
				(values) =>
					automotive
						.call("automotive_core.integration_hub.partners.rotate_inbound_secret", {
							partner: frm.docname,
							overlap_hours: values.overlap_hours,
						})
						.then((result) => {
							frappe.msgprint({
								title: __("New inbound secret"),
								indicator: "orange",
								message: `<p>${__("Give it to the partner over a secure channel. It is not shown again.")}</p><pre>${frappe.utils.escape_html(result.secret)}</pre>`,
							});
							frm.reload_doc();
						}),
				__("Rotate Inbound Secret"),
				__("Rotate")
			)
		);
	},
});

frappe.ui.form.on("Integration Outbound Message", {
	refresh(frm) {
		if (!["Failed", "Dead Letter"].includes(frm.doc.status)) return;
		if (!frappe.user.has_role(["IT Operator", "System Manager"])) return;
		frm.add_custom_button(__("Retry Delivery"), () =>
			automotive
				.call("automotive_core.integration_hub.outbox.retry", { name: frm.docname })
				.then(() => frm.reload_doc())
		);
	},
});

// Accessible names for the controls of Frappe's child-table grids, which carry none of their own
// (WCAG 2.1, 4.1.2): the row selection checkboxes and the grid's search inputs, labelled as they
// are rendered.
automotive.a11y = {
	label(root) {
		const within = (selector) => [
			...(root.matches?.(selector) ? [root] : []),
			...(root.querySelectorAll?.(selector) || []),
		];
		for (const box of within(".grid-row-check:not([aria-label])")) {
			const index = box.closest(".grid-row")?.querySelector(".row-index span")?.textContent?.trim();
			box.setAttribute(
				"aria-label",
				box.closest(".grid-heading-row")
					? __("Select all rows")
					: index
						? __("Select row {0}", [index])
						: __("Select row")
			);
		}
		for (const input of within(".grid-row .search input:not([aria-label])")) {
			const field = input.closest("[data-fieldname]")?.dataset.fieldname;
			const heading = field
				&& input
					.closest(".form-grid")
					?.querySelector(`.grid-heading-row [data-fieldname="${CSS.escape(field)}"] .static-area`)
					?.textContent?.trim();
			input.setAttribute("aria-label", __("Filter by {0}", [heading || __("Row number")]));
		}
	},
};
(() => {
	const observe = () =>
		new MutationObserver((mutations) => {
			for (const mutation of mutations) {
				for (const node of mutation.addedNodes) {
					if (node.nodeType === Node.ELEMENT_NODE) automotive.a11y.label(node);
				}
			}
		}).observe(document.body, { childList: true, subtree: true });
	if (document.body) observe();
	else document.addEventListener("DOMContentLoaded", observe);
})();
