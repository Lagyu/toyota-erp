// Desk actions for supply-chain documents.
frappe.provide("automotive.supply_chain");

const sc_call = (method, args) => frappe.call({ method, args, freeze: true }).then((r) => r.message);
const SC = "automotive_supply_chain.";

frappe.ui.form.on("Advance Shipping Notice", {
	refresh(frm) {
		if (frm.doc.docstatus === 1 && ["In Transit", "Arrived", "Partially Received"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Receive (Purchase Receipt)"), () =>
				sc_call(SC + "supplier_collaboration.shipments.make_purchase_receipt", { asn: frm.docname }).then((pr) =>
					frappe.set_route("Form", "Purchase Receipt", pr)
				)
			);
		}
	},
});

frappe.ui.form.on("Invoice Match Exception", {
	refresh(frm) {
		if (frm.doc.status !== "Open") return;
		for (const [label, method] of [[__("Accept Variance"), "accept_exception"], [__("Reject"), "reject_exception"]]) {
			frm.add_custom_button(label, () =>
				frappe.prompt({ fieldname: "note", fieldtype: "Small Text", label: __("Resolution note"), reqd: 1 }, (v) =>
					sc_call(SC + "procurement_controls.three_way_match." + method, { name: frm.docname, note: v.note }).then(() =>
						frm.reload_doc()
					)
				)
			);
		}
	},
});

frappe.ui.form.on("Dealer Order", {
	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.status === "Submitted") {
			frm.add_custom_button(__("Accept"), () =>
				sc_call(SC + "dealer_operations.orders.accept", { name: frm.docname }).then(() => frm.reload_doc())
			);
			frm.add_custom_button(__("Reject"), () =>
				frappe.prompt({ fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 }, (v) =>
					sc_call(SC + "dealer_operations.orders.reject", { name: frm.docname, reason: v.reason }).then(() => frm.reload_doc())
				)
			);
		}
	},
});

frappe.ui.form.on("Shipment Manifest", {
	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.delivery_status === "In Transit") {
			frm.add_custom_button(__("Record Proof Of Delivery"), () =>
				frappe.prompt(
					[
						{ fieldname: "received_by", fieldtype: "Data", label: __("Received by"), reqd: 1 },
						{ fieldname: "exception", fieldtype: "Small Text", label: __("Exception (if any)") },
					],
					(v) => sc_call(SC + "logistics.operations.record_delivery", { name: frm.docname, ...v }).then(() => frm.reload_doc())
				)
			);
		}
	},
});

frappe.ui.form.on("Cycle Count", {
	refresh(frm) {
		if (frm.doc.docstatus === 0 && !frm.is_new()) {
			frm.add_custom_button(__("Snapshot Expected Quantities"), () =>
				sc_call(SC + "logistics.operations.take_snapshot", { name: frm.docname }).then(() => frm.reload_doc())
			);
		}
	},
});

frappe.ui.form.on("Receipt Discrepancy", {
	refresh(frm) {
		if (frm.doc.status === "Open" && !frm.is_new()) {
			frm.add_custom_button(__("Resolve"), () =>
				frappe.prompt(
					[
						{ fieldname: "resolution", fieldtype: "Select", label: __("Resolution"), reqd: 1,
							options: ["Accept As Received", "Supplier Ships Balance", "Debit Note", "Return"].join("\n") },
						{ fieldname: "note", fieldtype: "Small Text", label: __("Note"), reqd: 1 },
					],
					(v) => sc_call(SC + "supplier_collaboration.shipments.resolve_discrepancy", { name: frm.docname, ...v }).then(() => frm.reload_doc())
				)
			);
		}
	},
});

frappe.ui.form.on("Kanban Signal", {
	refresh(frm) {
		// purchasing has been asked (material request) but no call-off order exists yet
		if (frm.doc.material_request && !frm.doc.purchase_order && ["Open", "Ordered", "Escalated"].includes(frm.doc.status)) {
			frm.add_custom_button(__("Release Call-Offs Now"), () =>
				frappe
					.call({ method: "automotive_supply_chain.supplier_collaboration.schedules.release_calloffs_now", freeze: true })
					.then((r) => {
						frappe.show_alert({ message: __("{0} call-off order(s) released", [(r.message || []).length]), indicator: "green" });
						frm.reload_doc();
					})
			);
		}
	},
});
