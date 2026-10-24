// Supplier portal (ERP-05): RFQs and quotations, orders and acknowledgements, delivery
// schedules, ASNs with shipment documents, receipts and discrepancies, invoices and
// remittances, notices. Everything is scoped to the signed-in user's supplier.

import {
	api, back, clear, counters, field, fmtDate, fmtMoney, fmtNumber, form, h, newKey, notice, section, shell,
	status, table,
} from "./core";

const M = "automotive_portals.api.supplier.";

export function mountSupplier(root, { party }) {
	shell(root, {
		title: __("Supplier Portal"),
		party,
		tabs: [
			{ key: "overview", label: __("Overview"), render: overview },
			{ key: "rfqs", label: __("RFQs & Quotations"), render: rfqs },
			{ key: "orders", label: __("Orders"), render: orders },
			{ key: "schedules", label: __("Delivery Schedules"), render: schedules },
			{ key: "shipments", label: __("Shipments (ASN)"), render: shipments },
			{ key: "receipts", label: __("Receipts & Discrepancies"), render: receipts },
			{ key: "payments", label: __("Invoices & Remittances"), render: payments },
			{ key: "notices", label: __("Notices"), render: notices },
		],
	});
}

async function overview({ go }) {
	const [d, latest] = await Promise.all([api.get(`${M}dashboard`), api.get(`${M}notices`)]);
	return h("div", {},
		counters([
			{ label: __("Orders awaiting acknowledgement"), value: d.orders_awaiting_ack, onClick: () => go("orders", { filter: "awaiting" }) },
			{ label: __("Open orders"), value: d.open_orders, onClick: () => go("orders") },
			{ label: __("Schedules to acknowledge"), value: d.schedules_to_acknowledge, onClick: () => go("schedules") },
			{ label: __("Open RFQs"), value: d.open_rfqs, onClick: () => go("rfqs") },
			{ label: __("Shipments in transit"), value: d.asns_in_transit, onClick: () => go("shipments") },
			{ label: __("Open discrepancies"), value: d.open_discrepancies, onClick: () => go("receipts") },
			{ label: __("Unread notices"), value: d.unread_notices, onClick: () => go("notices") },
		]),
		section(__("Latest notices"), table({
			caption: __("Latest notices"),
			rows: latest.slice(0, 5),
			columns: [
				{ label: __("Subject"), field: "subject" },
				{ label: __("Type"), render: (r) => __(r.notice_type) },
				{ label: __("Received"), render: (r) => fmtDate(r.creation) },
			],
			empty: __("No notices."),
		})));
}

// RFQs ---------------------------------------------------------------------------------

async function rfqs({ go, name, alert }) {
	if (name) return rfqDetail(name, go, alert);
	const [list, quotes] = await Promise.all([api.get(`${M}rfqs`), api.get(`${M}quotations`)]);
	return h("div", {},
		section(__("Requests for quotation"), table({
			caption: __("Requests for quotation"),
			rows: list,
			onOpen: (r) => go("rfqs", { name: r.name }),
			columns: [
				{ label: __("RFQ"), field: "name" },
				{ label: __("Date"), render: (r) => fmtDate(r.transaction_date) },
				{ label: __("Required by"), render: (r) => fmtDate(r.schedule_date) },
				{ label: __("Your quotation"), render: (r) => status(r.quote_status) },
			],
			empty: __("No requests for quotation."),
		})),
		section(__("Your quotations"), table({
			caption: __("Your quotations"),
			rows: quotes,
			columns: [
				{ label: __("Quotation"), field: "name" },
				{ label: __("Date"), render: (r) => fmtDate(r.transaction_date) },
				{ label: __("Valid till"), render: (r) => fmtDate(r.valid_till) },
				{ label: __("Total"), numeric: true, render: (r) => fmtMoney(r.grand_total, r.currency) },
				{ label: __("Status"), render: (r) => status(r.status) },
			],
			empty: __("No quotations yet."),
		})));
}

async function rfqDetail(name, go, alert) {
	const rfq = await api.get(`${M}rfq`, { name });
	const quoted = rfq.quotations.length > 0;
	const lines = rfq.items.map((item) => ({
		item,
		rate: field({ label: __("Unit price for {0}", [item.item_code]), name: `rate-${item.row}`, type: "number",
			min: "0", step: "any", required: !quoted }),
		qty: field({ label: __("Quantity for {0}", [item.item_code]), name: `qty-${item.row}`, type: "number",
			min: "0", step: "any", value: item.qty, required: !quoted }),
	}));
	const quoteForm = quoted ? h("p", { class: "hp-empty" }, __("You have quoted on this request.")) : form({
		legend: __("Submit your quotation"),
		submitLabel: __("Submit quotation"),
		region: alert,
		fields: [
			...lines.map((l) => h("div", { class: "hp-row" }, l.qty, l.rate)),
			field({ label: __("Valid till"), name: "valid_till", type: "date", required: true }),
			field({ label: __("Lead time (days)"), name: "lead_time_days", type: "number", min: "0", step: "1" }),
			field({ label: __("Remarks"), name: "remarks", type: "textarea" }),
		],
		onSubmit: async (data, key) => {
			const result = await api.post(`${M}submit_quotation`, {
				rfq_name: rfq.name,
				valid_till: data.get("valid_till"),
				lead_time_days: data.get("lead_time_days") || null,
				remarks: data.get("remarks") || null,
				lines: rfq.items.map((item) => ({ row: item.row, qty: data.get(`qty-${item.row}`),
					rate: data.get(`rate-${item.row}`) })),
			}, key);
			setTimeout(() => go("rfqs", { name: rfq.name }, { keepNotice: true }), 0);
			return __("Quotation {0} submitted.", [result.supplier_quotation]);
		},
	});
	return h("div", {},
		back(go, "rfqs", __("Back to RFQs")),
		section(__("Request {0}", [rfq.name]),
			h("dl", { class: "hp-facts" },
				h("dt", {}, __("Date")), h("dd", {}, fmtDate(rfq.date)),
				h("dt", {}, __("Required by")), h("dd", {}, fmtDate(rfq.required_by))),
			rfq.message ? h("p", { class: "hp-message" }, rfq.message) : null,
			table({
				caption: __("Requested items"),
				rows: rfq.items,
				columns: [
					{ label: __("Item"), field: "item_code" },
					{ label: __("Description"), field: "item_name" },
					{ label: __("Quantity"), numeric: true, render: (r) => `${fmtNumber(r.qty)} ${r.uom}` },
					{ label: __("Needed by"), render: (r) => fmtDate(r.schedule_date) },
				],
			})),
		quoteForm);
}

// Orders -------------------------------------------------------------------------------

async function orders({ go, name, filter, alert }) {
	if (name) return orderDetail(name, go, alert);
	const list = await api.get(`${M}orders`, { status: filter || null });
	const filterField = field({ label: __("Show"), name: "filter", type: "select", value: filter || "",
		options: [{ value: "", label: __("All open and recent orders") }, { value: "awaiting", label: __("Awaiting acknowledgement") }] });
	filterField.querySelector("select").addEventListener("change", (e) => go("orders", { filter: e.target.value }));
	return h("div", {}, filterField, table({
		caption: __("Purchase orders"),
		rows: list,
		onOpen: (r) => go("orders", { name: r.name }),
		columns: [
			{ label: __("Purchase Order"), field: "name" },
			{ label: __("Date"), render: (r) => fmtDate(r.transaction_date) },
			{ label: __("Type"), render: (r) => __(r.automotive_release_type || "Standard") },
			{ label: __("Total"), numeric: true, render: (r) => fmtMoney(r.grand_total, r.currency) },
			{ label: __("Received"), numeric: true, render: (r) => `${fmtNumber(r.per_received, 0)}%` },
			{ label: __("Acknowledgement"), render: (r) => status(r.automotive_ack_status) },
		],
		empty: __("No orders."),
	}));
}

async function orderDetail(name, go, alert) {
	const po = await api.get(`${M}order`, { name });
	const awaiting = !po.ack_status || po.ack_status === "Awaiting";
	const lineFields = po.lines.map((line) => ({
		line,
		qty: field({ label: __("Confirmed quantity for {0}", [line.item_code]), name: `qty-${line.row}`, type: "number",
			min: "0", step: "any", value: line.qty }),
		date: field({ label: __("Promised date for {0}", [line.item_code]), name: `date-${line.row}`, type: "date",
			value: line.promised_date || line.schedule_date }),
	}));
	const ackForm = awaiting ? form({
		legend: __("Acknowledge this order"),
		submitLabel: __("Send acknowledgement"),
		region: alert,
		fields: [
			field({ label: __("Response"), name: "response", type: "select", required: true, value: "Accepted", options: [
				{ value: "Accepted", label: __("Accept as ordered") },
				{ value: "Accepted With Changes", label: __("Accept with revised quantities or dates") },
				{ value: "Rejected", label: __("Reject") }] }),
			field({ label: __("Your reference"), name: "supplier_reference" }),
			...lineFields.map((l) => h("div", { class: "hp-row" }, l.qty, l.date)),
		],
		onSubmit: async (data, key) => {
			const response = data.get("response");
			const lines = response === "Rejected" ? [] : po.lines.map((line) => ({
				po_item: line.row, confirmed_qty: data.get(`qty-${line.row}`), promised_date: data.get(`date-${line.row}`),
			}));
			const result = await api.post(`${M}acknowledge`, { purchase_order: po.name, response,
				supplier_reference: data.get("supplier_reference") || null, lines }, key);
			setTimeout(() => go("orders", { name: po.name }, { keepNotice: true }), 0);
			return __("Acknowledgement {0} sent ({1}).", [result.acknowledgement, __(result.ack_type)]);
		},
	}) : null;
	return h("div", {},
		back(go, "orders", __("Back to orders")),
		section(__("Order {0}", [po.name]),
			h("dl", { class: "hp-facts" },
				h("dt", {}, __("Date")), h("dd", {}, fmtDate(po.date)),
				h("dt", {}, __("Total")), h("dd", {}, fmtMoney(po.grand_total, po.currency)),
				h("dt", {}, __("Acknowledgement")), h("dd", {}, status(po.ack_status))),
			table({
				caption: __("Order lines"),
				rows: po.lines,
				columns: [
					{ label: __("Item"), field: "item_code" },
					{ label: __("Description"), field: "item_name" },
					{ label: __("Ordered"), numeric: true, render: (r) => `${fmtNumber(r.qty)} ${r.uom}` },
					{ label: __("Price"), numeric: true, render: (r) => fmtMoney(r.rate, po.currency) },
					{ label: __("Required by"), render: (r) => fmtDate(r.schedule_date) },
					{ label: __("Promised"), render: (r) => fmtDate(r.promised_date) },
					{ label: __("Received"), numeric: true, render: (r) => fmtNumber(r.received_qty) },
				],
			})),
		ackForm);
}

// Schedules ------------------------------------------------------------------------------

async function schedules({ go, name, alert }) {
	if (name) {
		const s = await api.get(`${M}schedule`, { name });
		const ack = s.status === "Published"
			? h("button", { type: "button", class: "hp-button hp-primary", onclick: async (e) => {
				e.target.disabled = true;
				try {
					await api.post(`${M}acknowledge_schedule`, { name: s.name });
					notice(alert, __("Schedule {0} acknowledged.", [s.name]), "success");
					go("schedules", { name: s.name }, { keepNotice: true });
				} catch (err) {
					e.target.disabled = false;
					notice(alert, err.message, "error");
				}
			} }, __("Acknowledge schedule"))
			: null;
		return h("div", {}, back(go, "schedules", __("Back to schedules")),
			section(__("Schedule {0} (version {1})", [s.name, s.version]), h("p", {}, status(s.status)), ack,
				table({
					caption: __("Schedule lines"),
					rows: s.lines,
					columns: [
						{ label: __("Item"), field: "item_code" },
						{ label: __("Delivery date"), render: (r) => fmtDate(r.delivery_date) },
						{ label: __("Quantity"), numeric: true, render: (r) => fmtNumber(r.qty) },
						{ label: __("Firm"), render: (r) => (r.firm ? __("Firm") : __("Forecast")) },
						{ label: __("Purchase Order"), field: "purchase_order" },
					],
				})));
	}
	const list = await api.get(`${M}schedules`);
	return table({
		caption: __("Delivery schedules"),
		rows: list,
		onOpen: (r) => go("schedules", { name: r.name }),
		columns: [
			{ label: __("Schedule"), field: "name" },
			{ label: __("Plant"), field: "plant" },
			{ label: __("Version"), numeric: true, field: "schedule_version" },
			{ label: __("Horizon"), render: (r) => `${fmtDate(r.horizon_start)} – ${fmtDate(r.horizon_end)}` },
			{ label: __("Status"), render: (r) => status(r.status) },
		],
		empty: __("No delivery schedules."),
	});
}

// Shipments ------------------------------------------------------------------------------

async function shipments({ go, name, create, alert }) {
	if (name) return asnDetail(name, go, alert);
	if (create) return asnForm(go, alert);
	const list = await api.get(`${M}asns`);
	return h("div", {},
		h("button", { type: "button", class: "hp-button hp-primary", onclick: () => go("shipments", { create: true }) },
			__("Announce a shipment")),
		table({
			caption: __("Advance shipping notices"),
			rows: list,
			onOpen: (r) => go("shipments", { name: r.name }),
			columns: [
				{ label: __("ASN"), field: "name" },
				{ label: __("Your ASN number"), field: "supplier_asn_no" },
				{ label: __("Shipped"), render: (r) => fmtDate(r.ship_date) },
				{ label: __("Expected"), render: (r) => fmtDate(r.expected_arrival) },
				{ label: __("Carrier"), field: "carrier" },
				{ label: __("Status"), render: (r) => status(r.status) },
			],
			empty: __("No shipments announced."),
		}));
}

async function asnForm(go, alert) {
	const lines = await api.get(`${M}open_lines`);
	if (!lines.length) {
		return h("div", {}, back(go, "shipments", __("Back to shipments")),
			h("p", { class: "hp-empty" }, __("There are no open order lines to ship.")));
	}
	const today = new Date().toISOString().slice(0, 10);
	const rows = lines.map((line, i) => {
		const include = h("input", { type: "checkbox", name: `include-${i}`, id: `include-${i}` });
		return h("tr", {},
			h("td", {}, include, h("label", { for: `include-${i}`, class: "hp-sr" }, __("Ship {0}", [line.item_code]))),
			h("td", {}, line.purchase_order),
			h("td", {}, line.item_code, h("br"), h("small", {}, line.item_name)),
			h("td", { class: "hp-num" }, fmtNumber(line.open_qty)),
			h("td", {}, h("input", { type: "number", name: `qty-${i}`, min: "0", step: "any", value: line.open_qty,
				"aria-label": __("Quantity for {0}", [line.item_code]) })),
			h("td", {}, line.has_batch_no ? h("input", { type: "text", name: `lot-${i}`,
				"aria-label": __("Lot for {0}", [line.item_code]) }) : "–"),
			h("td", {}, line.has_serial_no ? h("textarea", { name: `serials-${i}`, rows: 2,
				"aria-label": __("Serial numbers for {0}, one per line", [line.item_code]) }) : "–"));
	});
	const lineTable = h("div", { class: "hp-table-wrap", role: "region", "aria-label": __("Open order lines"), tabindex: "0" },
		h("table", { class: "hp-table" }, h("caption", {}, __("Open order lines")),
			h("thead", {}, h("tr", {}, [__("Ship"), __("Purchase Order"), __("Item"), __("Open"), __("Quantity"), __("Lot"), __("Serial numbers")]
				.map((label) => h("th", { scope: "col" }, label)))),
			h("tbody", {}, rows)));
	return h("div", {}, back(go, "shipments", __("Back to shipments")), form({
		legend: __("Announce a shipment"),
		submitLabel: __("Send ASN"),
		region: alert,
		fields: [
			field({ label: __("Your ASN number"), name: "supplier_asn_no", required: true }),
			h("div", { class: "hp-row" },
				field({ label: __("Ship date"), name: "ship_date", type: "date", required: true, value: today }),
				field({ label: __("Expected arrival"), name: "expected_arrival", type: "date", required: true, value: today })),
			h("div", { class: "hp-row" },
				field({ label: __("Carrier"), name: "carrier" }),
				field({ label: __("Tracking / waybill number"), name: "tracking_no" }),
				field({ label: __("Packages"), name: "packages", type: "number", min: "0", step: "1" })),
			lineTable,
			field({ label: __("Shipment documents (packing list, certificates)"), name: "documents", type: "file",
				attrs: { multiple: true, accept: ".pdf,.png,.jpg,.jpeg,.csv,.xlsx" } }),
		],
		onSubmit: async (data, key) => {
			const chosen = lines.map((line, i) => ({ line, i })).filter(({ i }) => data.get(`include-${i}`));
			if (!chosen.length) throw new Error(__("Select at least one order line to ship."));
			const payload = chosen.map(({ line, i }) => ({
				purchase_order: line.purchase_order, po_item: line.po_item, item_code: line.item_code,
				qty: data.get(`qty-${i}`), supplier_lot: data.get(`lot-${i}`) || null,
				serial_nos: String(data.get(`serials-${i}`) || "").split(/\s+/).filter(Boolean),
			}));
			const result = await api.post(`${M}create_asn`, {
				supplier_asn_no: data.get("supplier_asn_no"), ship_date: data.get("ship_date"),
				expected_arrival: data.get("expected_arrival"), carrier: data.get("carrier") || null,
				tracking_no: data.get("tracking_no") || null, packages: data.get("packages") || null, lines: payload,
			}, key);
			const files = data.getAll("documents").filter((f) => f && f.size);
			for (const file of files) await upload(file, result.asn);
			setTimeout(() => go("shipments", { name: result.asn }, { keepNotice: true }), 0);
			return __("Shipment {0} announced.", [result.asn]);
		},
	}));
}

async function upload(file, asn) {
	const body = new FormData();
	body.append("file", file, file.name);
	body.append("doctype", "Advance Shipping Notice");
	body.append("docname", asn);
	body.append("is_private", "1");
	const response = await fetch("/api/method/upload_file", {
		method: "POST", body, credentials: "same-origin",
		headers: { "X-Frappe-CSRF-Token": (window.frappe && window.frappe.csrf_token) || "", "Idempotency-Key": newKey() },
	});
	if (!response.ok) throw new Error(__("{0} could not be uploaded.", [file.name]));
}

async function asnDetail(name, go, alert) {
	const asn = await api.get(`${M}asn`, { name });
	const uploader = h("input", { type: "file", id: "hp-asn-upload", multiple: true, accept: ".pdf,.png,.jpg,.jpeg,.csv,.xlsx" });
	const uploadButton = h("button", { type: "button", class: "hp-button hp-secondary", onclick: async () => {
		try {
			for (const file of uploader.files) await upload(file, asn.name);
			notice(alert, __("Documents uploaded."), "success");
			go("shipments", { name: asn.name });
		} catch (err) {
			notice(alert, err.message, "error");
		}
	} }, __("Upload"));
	return h("div", {}, back(go, "shipments", __("Back to shipments")),
		section(__("Shipment {0}", [asn.name]),
			h("dl", { class: "hp-facts" },
				h("dt", {}, __("Your ASN number")), h("dd", {}, asn.supplier_asn_no),
				h("dt", {}, __("Status")), h("dd", {}, status(asn.status)),
				h("dt", {}, __("Shipped")), h("dd", {}, fmtDate(asn.ship_date)),
				h("dt", {}, __("Expected")), h("dd", {}, fmtDate(asn.expected_arrival)),
				h("dt", {}, __("Carrier")), h("dd", {}, asn.carrier || "–"),
				h("dt", {}, __("Tracking / waybill number")), h("dd", {}, asn.tracking_no || "–")),
			table({
				caption: __("Shipped lines"),
				rows: asn.lines,
				columns: [
					{ label: __("Purchase Order"), field: "purchase_order" },
					{ label: __("Item"), field: "item_code" },
					{ label: __("Shipped"), numeric: true, render: (r) => fmtNumber(r.qty) },
					{ label: __("Received"), numeric: true, render: (r) => fmtNumber(r.received_qty) },
					{ label: __("Lot"), field: "supplier_lot" },
				],
			})),
		section(__("Shipment documents"),
			asn.documents.length
				? h("ul", {}, asn.documents.map((d) => h("li", {}, h("a", { href: d.file_url, target: "_blank", rel: "noopener" }, d.file_name))))
				: h("p", { class: "hp-empty" }, __("No documents attached.")),
			h("div", { class: "hp-field" }, h("label", { for: "hp-asn-upload" }, __("Add documents")), uploader), uploadButton));
}

// Receipts / payments / notices -------------------------------------------------------------

async function receipts() {
	const [list, issues] = await Promise.all([api.get(`${M}receipts`), api.get(`${M}discrepancies`)]);
	return h("div", {},
		section(__("Receipt discrepancies"), table({
			caption: __("Receipt discrepancies"),
			rows: issues,
			columns: [
				{ label: __("Discrepancy"), field: "name" },
				{ label: __("Item"), field: "item_code" },
				{ label: __("Type"), render: (r) => __(r.discrepancy_type) },
				{ label: __("Expected"), numeric: true, render: (r) => fmtNumber(r.expected_qty) },
				{ label: __("Received"), numeric: true, render: (r) => fmtNumber(r.received_qty) },
				{ label: __("Rejected"), numeric: true, render: (r) => fmtNumber(r.rejected_qty) },
				{ label: __("Status"), render: (r) => status(r.status) },
				{ label: __("Resolution"), render: (r) => __(r.resolution || "") },
			],
			empty: __("No discrepancies."),
		})),
		section(__("Goods receipts"), table({
			caption: __("Goods receipts"),
			rows: list,
			columns: [
				{ label: __("Receipt"), field: "name" },
				{ label: __("Date"), render: (r) => fmtDate(r.posting_date) },
				{ label: __("ASN"), field: "automotive_asn" },
				{ label: __("Accepted"), numeric: true, render: (r) => fmtNumber(r.lines.reduce((s, l) => s + Number(l.qty || 0), 0)) },
				{ label: __("Rejected"), numeric: true, render: (r) => fmtNumber(r.lines.reduce((s, l) => s + Number(l.rejected_qty || 0), 0)) },
			],
			empty: __("No receipts yet."),
		})));
}

async function payments() {
	const [invoices, remittances] = await Promise.all([api.get(`${M}invoices_status`), api.get(`${M}remittances`)]);
	return h("div", {},
		section(__("Invoice status"), table({
			caption: __("Invoice status"),
			rows: invoices,
			columns: [
				{ label: __("Your invoice"), field: "bill_no" },
				{ label: __("Invoice date"), render: (r) => fmtDate(r.bill_date) },
				{ label: __("Amount"), numeric: true, render: (r) => fmtNumber(r.grand_total) },
				{ label: __("Outstanding"), numeric: true, render: (r) => fmtNumber(r.outstanding_amount) },
				{ label: __("Due"), render: (r) => fmtDate(r.due_date) },
				{ label: __("Matching"), render: (r) => status(r.automotive_match_status) },
				{ label: __("Status"), render: (r) => status(r.status) },
			],
			empty: __("No invoices."),
		})),
		section(__("Remittance advice"), table({
			caption: __("Remittance advice"),
			rows: remittances,
			columns: [
				{ label: __("Payment"), field: "name" },
				{ label: __("Date"), render: (r) => fmtDate(r.posting_date) },
				{ label: __("Amount"), numeric: true, render: (r) => fmtMoney(r.paid_amount, r.paid_from_account_currency) },
				{ label: __("Reference"), field: "reference_no" },
				{ label: __("Invoices settled"), render: (r) => r.invoices.map((i) => i.supplier_invoice || i.reference_name).join(", ") },
			],
			empty: __("No payments yet."),
		})));
}

async function notices({ go, alert }) {
	const list = await api.get(`${M}notices`);
	return table({
		caption: __("Notices"),
		rows: list,
		columns: [
			{ label: __("Subject"), field: "subject" },
			{ label: __("Type"), render: (r) => __(r.notice_type) },
			{ label: __("Message"), field: "message" },
			{ label: __("Received"), render: (r) => fmtDate(r.creation) },
			{ label: __("Read"), render: (r) => (r.read_at ? fmtDate(r.read_at)
				: h("button", { type: "button", class: "hp-button hp-secondary", onclick: async () => {
					try {
						await api.post(`${M}mark_notice_read`, { name: r.name });
						go("notices");
					} catch (err) {
						notice(alert, err.message, "error");
					}
				} }, __("Mark as read"))) },
		],
		empty: __("No notices."),
	});
}

