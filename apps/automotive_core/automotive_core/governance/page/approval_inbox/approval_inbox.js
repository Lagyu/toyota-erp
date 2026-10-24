// Copyright (c) 2026, Toyota ERP Clone contributors
// License: GNU General Public License v3. See license.txt
//
// Approval inbox (ERP-10, ERP-12): every pending approval step the signed-in user may
// decide (by role or valid delegation), oldest first, with approve / reject in place.
// The server re-checks maker/checker, distinct approvers and the document hash on decide.

frappe.pages["approval-inbox"].on_page_load = (wrapper) => {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Approval Inbox"), single_column: true });
	const body = $('<div class="approval-inbox"></div>').appendTo(page.main);
	page.set_primary_action(__("Refresh"), () => render(), "refresh");

	const decide = (request, decision) =>
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
				frappe
					.call({
						method: "automotive_core.governance.approvals.decide",
						args: { request, decision, comment: values.comment },
						freeze: true,
					})
					.then(() => {
						frappe.show_alert({ message: __("{0}: {1}", [request, __(decision)]), indicator: "green" });
						render();
					}),
			decision === "Approve" ? __("Approve {0}", [request]) : __("Reject {0}", [request]),
			decision === "Approve" ? __("Approve") : __("Reject")
		);

	async function render() {
		body.empty().append(`<p class="text-muted">${__("Loading…")}</p>`);
		const rows = (await frappe.call("automotive_core.governance.approvals.get_inbox")).message || [];
		body.empty();
		if (!rows.length) {
			body.append(`<p class="text-muted">${__("Nothing is waiting for your decision.")}</p>`);
			return;
		}
		const table = $(`<table class="table table-bordered">
			<caption class="sr-only">${__("Pending approvals")}</caption>
			<thead><tr>
				<th scope="col">${__("Request")}</th><th scope="col">${__("Document")}</th>
				<th scope="col">${__("Company")}</th><th scope="col" class="text-right">${__("Amount")}</th>
				<th scope="col">${__("Step")}</th><th scope="col">${__("Requested By")}</th>
				<th scope="col">${__("Requested At")}</th><th scope="col">${__("Decision")}</th>
			</tr></thead><tbody></tbody></table>`).appendTo(body);
		for (const r of rows) {
			const tr = $("<tr></tr>").appendTo(table.find("tbody"));
			$("<td></td>")
				.append($("<a></a>").attr("href", frappe.utils.get_form_link("Approval Request", r.name)).text(r.name))
				.appendTo(tr);
			$("<td></td>")
				.append(
					$("<a></a>")
						.attr("href", frappe.utils.get_form_link(r.reference_doctype, r.reference_name))
						.text(`${__(r.reference_doctype)} ${r.reference_name}`)
				)
				.appendTo(tr);
			$("<td></td>").text(r.company || "").appendTo(tr);
			$('<td class="text-right"></td>').text(r.amount ? format_currency(r.amount, r.currency) : "").appendTo(tr);
			$("<td></td>").text(r.current_step).appendTo(tr);
			$("<td></td>").text(r.requested_by).appendTo(tr);
			$("<td></td>").text(frappe.datetime.str_to_user(r.requested_at)).appendTo(tr);
			const actions = $("<td></td>").appendTo(tr);
			$('<button class="btn btn-xs btn-primary mr-2"></button>')
				.text(__("Approve"))
				.on("click", () => decide(r.name, "Approve"))
				.appendTo(actions);
			$('<button class="btn btn-xs btn-default"></button>')
				.text(__("Reject"))
				.on("click", () => decide(r.name, "Reject"))
				.appendTo(actions);
		}
	}
	render();
};
