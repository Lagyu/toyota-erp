// Dealer portal (ERP-08): configure and order vehicles, follow orders and request changes,
// see allocated inventory and deliveries (with acceptance), invoices and support requests.
// Everything is scoped to the signed-in user's dealership.

import {
	api, back, clear, counters, field, fmtDate, fmtMoney, fmtNumber, form, h, notice, section, shell, status, table,
} from "./core";

const M = "automotive_portals.api.dealer.";
const cart = [];

export function mountDealer(root, { party }) {
	shell(root, {
		title: __("Dealer Portal"),
		party,
		tabs: [
			{ key: "overview", label: __("Overview"), render: overview },
			{ key: "configure", label: __("Configure & Order"), render: configure },
			{ key: "orders", label: __("Orders"), render: orders },
			{ key: "inventory", label: __("Allocated Vehicles"), render: inventory },
			{ key: "deliveries", label: __("Deliveries"), render: deliveries },
			{ key: "invoices", label: __("Invoices"), render: invoices },
			{ key: "support", label: __("Support"), render: support },
		],
	});
}

async function overview({ go }) {
	const [d, balance] = await Promise.all([api.get(`${M}dashboard`), api.get(`${M}outstanding_balance`)]);
	return h("div", {},
		counters([
			{ label: __("Open orders"), value: d.open_orders, onClick: () => go("orders") },
			{ label: __("Allocated vehicles"), value: d.allocated_vehicles, onClick: () => go("inventory") },
			{ label: __("Vehicles in delivery"), value: d.in_delivery, onClick: () => go("deliveries") },
			{ label: __("Open invoices"), value: d.open_invoices, onClick: () => go("invoices") },
			{ label: __("Open support requests"), value: d.open_support_requests, onClick: () => go("support") },
		]),
		section(__("Outstanding balance"), Object.keys(balance).length
			? h("ul", {}, Object.entries(balance).map(([currency, amount]) => h("li", {}, fmtMoney(amount, currency))))
			: h("p", { class: "hp-empty" }, __("Nothing outstanding."))));
}

// Configure & order ------------------------------------------------------------------------

async function configure({ go, alert }) {
	const models = await api.get(`${M}models`);
	if (!models.length) return h("p", { class: "hp-empty" }, __("No models are open for ordering."));
	const result = h("div", { class: "hp-config-result", role: "status", "aria-live": "polite" });
	const options = h("div", { class: "hp-options" });
	const modelField = field({ label: __("Model"), name: "vehicle_model", type: "select", required: true,
		options: models.map((m) => ({ value: m.name, label: `${m.name} – ${m.model_name}` })) });
	const modelSelect = modelField.querySelector("select");
	const qtyField = field({ label: __("Quantity"), name: "qty", type: "number", min: "1", step: "1", value: 1, required: true });
	let checked = null;

	function renderOptions() {
		const model = models.find((m) => m.name === modelSelect.value);
		clear(options);
		clear(result);
		checked = null;
		for (const group of model.options) {
			options.appendChild(field({
				label: __(group.attribute), name: `opt-${group.attribute}`, type: "select", required: !!group.required,
				options: [{ value: "", label: group.required ? __("Choose…") : __("None") },
					...group.values.map((v) => ({ value: v, label: __(v) }))],
			}));
		}
	}
	modelSelect.addEventListener("change", renderOptions);
	renderOptions();

	function selections() {
		const model = models.find((m) => m.name === modelSelect.value);
		const out = {};
		for (const group of model.options) {
			const value = options.querySelector(`[name="opt-${CSS.escape(group.attribute)}"]`).value;
			if (value) out[group.attribute] = value;
		}
		return out;
	}

	const checkButton = h("button", { type: "button", class: "hp-button hp-secondary", onclick: async () => {
		clear(result);
		checked = null;
		try {
			const qty = qtyField.querySelector("input").value || 1;
			const r = await api.get(`${M}check_configuration`, { vehicle_model: modelSelect.value,
				selections: selections(), qty });
			if (!r.valid) {
				result.appendChild(h("div", { class: "hp-notice hp-notice-error" }, __("This configuration cannot be built:"),
					h("ul", {}, r.violations.map((v) => h("li", {}, v.message)))));
				return;
			}
			checked = { vehicle_model: modelSelect.value, selections: selections(), qty: Number(qty), ...r };
			result.appendChild(h("div", { class: "hp-notice hp-notice-success" },
				__("Valid configuration {0}: {1} each, earliest delivery {2}.", [r.variant, fmtMoney(r.list_price, r.currency),
					fmtDate(r.promise_date)])));
		} catch (e) {
			notice(alert, e.message, "error");
		}
	} }, __("Check configuration"));

	const addButton = h("button", { type: "button", class: "hp-button hp-primary", onclick: () => {
		if (!checked) {
			notice(alert, __("Check the configuration first."), "error");
			return;
		}
		cart.push(checked);
		checked = null;
		go("configure");
	} }, __("Add to order"));

	const cartTable = table({
		caption: __("Vehicles in this order"),
		rows: cart.map((line, i) => ({ ...line, i })),
		columns: [
			{ label: __("Model"), field: "vehicle_model" },
			{ label: __("Variant"), field: "variant" },
			{ label: __("Quantity"), numeric: true, render: (r) => fmtNumber(r.qty, 0) },
			{ label: __("Price"), numeric: true, render: (r) => fmtMoney(r.list_price * r.qty, r.currency) },
			{ label: __("Earliest delivery"), render: (r) => fmtDate(r.promise_date) },
			{ label: __("Remove"), render: (r) => h("button", { type: "button", class: "hp-button hp-secondary",
				onclick: () => { cart.splice(r.i, 1); go("configure"); } }, __("Remove")) },
		],
		empty: __("No vehicles added yet."),
	});

	const orderForm = cart.length ? form({
		legend: __("Place the order"),
		submitLabel: __("Place order"),
		region: alert,
		fields: [
			field({ label: __("Your order reference"), name: "dealer_reference", required: true }),
			field({ label: __("Requested delivery date"), name: "requested_delivery_date", type: "date", required: true }),
			field({ label: __("Order type"), name: "order_type", type: "select", value: "Customer Order", options: [
				{ value: "Customer Order", label: __("Customer order") }, { value: "Stock Order", label: __("Stock order") },
				{ value: "Demonstrator", label: __("Demonstrator") }] }),
		],
		onSubmit: async (data, key) => {
			const r = await api.post(`${M}place_order`, {
				dealer_reference: data.get("dealer_reference"),
				requested_delivery_date: data.get("requested_delivery_date"),
				order_type: data.get("order_type"),
				lines: cart.map((line) => ({ vehicle_model: line.vehicle_model, selections: line.selections, qty: line.qty })),
			}, key);
			cart.length = 0;
			setTimeout(() => go("orders", { name: r.dealer_order }, { keepNotice: true }), 0);
			return __("Order {0} placed ({1}).", [r.dealer_order, fmtMoney(r.total, r.currency)]);
		},
	}) : null;

	return h("div", {},
		section(__("Configure a vehicle"), modelField, options, qtyField,
			h("div", { class: "hp-actions" }, checkButton, addButton), result),
		section(__("Order"), cartTable, orderForm));
}

// Orders -----------------------------------------------------------------------------------

async function orders({ go, name, alert }) {
	if (name) return orderDetail(name, go, alert);
	const list = await api.get(`${M}orders`);
	return table({
		caption: __("Dealer orders"),
		rows: list,
		onOpen: (r) => go("orders", { name: r.name }),
		columns: [
			{ label: __("Order"), field: "name" },
			{ label: __("Your reference"), field: "dealer_reference" },
			{ label: __("Type"), render: (r) => __(r.order_type) },
			{ label: __("Requested"), render: (r) => fmtDate(r.requested_delivery_date) },
			{ label: __("Promised"), render: (r) => fmtDate(r.promised_date) },
			{ label: __("Total"), numeric: true, render: (r) => fmtMoney(r.total, r.currency) },
			{ label: __("Status"), render: (r) => status(r.status) },
		],
		empty: __("No orders yet."),
	});
}

async function orderDetail(name, go, alert) {
	const o = await api.get(`${M}order`, { name });
	const changeable = o.status === "Accepted";
	const changeForm = changeable ? form({
		legend: __("Request a change"),
		submitLabel: __("Send change request"),
		region: alert,
		fields: [
			field({ label: __("Change"), name: "change_type", type: "select", required: true, options: [
				{ value: "Change Quantity", label: __("Change quantity") },
				{ value: "Change Delivery Date", label: __("Change delivery date") },
				{ value: "Cancel Order", label: __("Cancel the order") }] }),
			field({ label: __("Order line"), name: "item_row", type: "select",
				options: o.lines.map((l) => ({ value: l.idx, label: `${l.idx}: ${l.model} ${l.variant || ""}` })) }),
			h("div", { class: "hp-row" },
				field({ label: __("New quantity"), name: "new_qty", type: "number", min: "0", step: "1" }),
				field({ label: __("New delivery date"), name: "new_delivery_date", type: "date" })),
			field({ label: __("Reason"), name: "reason", type: "textarea", required: true }),
		],
		onSubmit: async (data, key) => {
			const r = await api.post(`${M}request_change`, {
				dealer_order: o.name, change_type: data.get("change_type"), item_row: data.get("item_row"),
				new_qty: data.get("new_qty") || null, new_delivery_date: data.get("new_delivery_date") || null,
				reason: data.get("reason"),
			}, key);
			setTimeout(() => go("orders", { name: o.name }, { keepNotice: true }), 0);
			return __("Change {0}: {1}", [r.change, r.effect || __(r.status)]);
		},
	}) : null;
	return h("div", {}, back(go, "orders", __("Back to orders")),
		section(__("Order {0}", [o.name]),
			h("dl", { class: "hp-facts" },
				h("dt", {}, __("Your reference")), h("dd", {}, o.reference),
				h("dt", {}, __("Status")), h("dd", {}, status(o.status)),
				h("dt", {}, __("Promised")), h("dd", {}, fmtDate(o.promised_date) || "–"),
				h("dt", {}, __("Total")), h("dd", {}, fmtMoney(o.total, o.currency))),
			table({
				caption: __("Ordered vehicles"),
				rows: o.lines,
				columns: [
					{ label: __("Line"), numeric: true, field: "idx" },
					{ label: __("Model"), field: "model" },
					{ label: __("Variant"), field: "variant" },
					{ label: __("Quantity"), numeric: true, render: (r) => fmtNumber(r.qty, 0) },
					{ label: __("Amount"), numeric: true, render: (r) => fmtMoney(r.amount, o.currency) },
				],
			}),
			table({
				caption: __("Vehicles"),
				rows: o.vehicles,
				columns: [
					{ label: __("VIN"), field: "name" },
					{ label: __("Variant"), field: "variant_item" },
					{ label: __("Production"), render: (r) => __(r.production_status || "") },
					{ label: __("Quality"), render: (r) => __(r.quality_status || "") },
					{ label: __("Shipment"), render: (r) => status(r.shipment_status) },
				],
				empty: __("No vehicles assigned yet."),
			}),
			table({
				caption: __("Change requests"),
				rows: o.changes,
				columns: [
					{ label: __("Change"), field: "name" },
					{ label: __("Type"), render: (r) => __(r.change_type) },
					{ label: __("Status"), render: (r) => status(r.status) },
					{ label: __("Effect"), field: "effect" },
				],
				empty: __("No change requests."),
			})),
		changeForm);
}

// Inventory / deliveries / invoices / support -------------------------------------------------

async function inventory() {
	const list = await api.get(`${M}inventory`);
	return table({
		caption: __("Vehicles allocated to you"),
		rows: list,
		columns: [
			{ label: __("VIN"), field: "name" },
			{ label: __("Variant"), field: "variant_item" },
			{ label: __("Inventory"), render: (r) => __(r.inventory_status || "") },
			{ label: __("Shipment"), render: (r) => status(r.shipment_status) },
			{ label: __("Invoice"), render: (r) => __(r.invoice_status || "") },
			{ label: __("Warranty start"), render: (r) => fmtDate(r.warranty_start_date) },
		],
		empty: __("No vehicles allocated."),
	});
}

async function deliveries({ go, alert, accept }) {
	const list = await api.get(`${M}deliveries`);
	if (accept) {
		const dn = list.find((d) => d.name === accept);
		return h("div", {}, back(go, "deliveries", __("Back to deliveries")), form({
			legend: __("Accept delivery {0}", [accept]),
			submitLabel: __("Confirm"),
			region: alert,
			fields: [
				h("p", {}, __("Vehicles: {0}", [(dn ? dn.vehicles : []).join(", ")])),
				field({ label: __("Condition"), name: "condition", type: "select", required: true, value: "Accepted", options: [
					{ value: "Accepted", label: __("Accepted in good condition") },
					{ value: "Accepted With Damage", label: __("Accepted with damage") },
					{ value: "Rejected", label: __("Rejected") }] }),
				field({ label: __("Damage or rejection notes"), name: "damage_notes", type: "textarea",
					help: __("Required when a vehicle is damaged or rejected.") }),
			],
			onSubmit: async (data, key) => {
				const condition = data.get("condition");
				if (condition !== "Accepted" && !data.get("damage_notes")) throw new Error(__("Describe the damage or the reason."));
				const r = await api.post(`${M}accept_delivery`, { delivery_note: accept, condition,
					damage_notes: data.get("damage_notes") || null }, key);
				setTimeout(() => go("deliveries", {}, { keepNotice: true }), 0);
				return __("Delivery {0} recorded as {1}.", [accept, __(r.condition)]);
			},
		}));
	}
	return table({
		caption: __("Deliveries"),
		rows: list,
		columns: [
			{ label: __("Delivery"), field: "name" },
			{ label: __("Date"), render: (r) => fmtDate(r.posting_date) },
			{ label: __("Vehicles"), render: (r) => r.vehicles.join(", ") },
			{ label: __("Transport"), render: (r) => status(r.delivery_status) },
			{ label: __("Your acceptance"), render: (r) => (r.automotive_acceptance_status && r.automotive_acceptance_status !== "Pending"
				? status(r.automotive_acceptance_status)
				: h("button", { type: "button", class: "hp-button hp-primary", onclick: () => go("deliveries", { accept: r.name }) },
					__("Record acceptance"))) },
		],
		empty: __("No deliveries yet."),
	});
}

async function invoices() {
	const list = await api.get(`${M}invoices`);
	return table({
		caption: __("Invoices and credit notes"),
		rows: list,
		columns: [
			{ label: __("Invoice"), field: "name" },
			{ label: __("Date"), render: (r) => fmtDate(r.posting_date) },
			{ label: __("Type"), render: (r) => (r.is_return ? __("Credit note") : __("Invoice")) },
			{ label: __("Amount"), numeric: true, render: (r) => fmtMoney(r.grand_total, r.currency) },
			{ label: __("Outstanding"), numeric: true, render: (r) => fmtMoney(r.outstanding_amount, r.currency) },
			{ label: __("Due"), render: (r) => fmtDate(r.due_date) },
			{ label: __("Status"), render: (r) => status(r.status) },
		],
		empty: __("No invoices yet."),
	});
}

async function support({ go, alert }) {
	const [list, vehicles] = await Promise.all([api.get(`${M}support_requests`), api.get(`${M}inventory`)]);
	return h("div", {},
		form({
			legend: __("New support request"),
			submitLabel: __("Send request"),
			region: alert,
			fields: [
				field({ label: __("Subject"), name: "subject", required: true, attrs: { maxlength: 140 } }),
				field({ label: __("Vehicle (optional)"), name: "vehicle", type: "select",
					options: [{ value: "", label: __("Not about a specific vehicle") }, ...vehicles.map((v) => ({ value: v.name, label: v.name }))] }),
				field({ label: __("Description"), name: "description", type: "textarea", required: true }),
			],
			onSubmit: async (data, key) => {
				const r = await api.post(`${M}create_support_request`, { subject: data.get("subject"),
					description: data.get("description"), vehicle: data.get("vehicle") || null }, key);
				setTimeout(() => go("support", {}, { keepNotice: true }), 0);
				return __("Support request {0} created.", [r.issue]);
			},
		}),
		section(__("Your support requests"), table({
			caption: __("Your support requests"),
			rows: list,
			columns: [
				{ label: __("Request"), field: "name" },
				{ label: __("Subject"), field: "subject" },
				{ label: __("Opened"), render: (r) => fmtDate(r.opening_date) },
				{ label: __("Priority"), render: (r) => __(r.priority || "") },
				{ label: __("Status"), render: (r) => status(r.status) },
			],
			empty: __("No support requests."),
		})));
}
