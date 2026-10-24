// Shared building blocks for the partner portals.
//
// * Every request goes to a party-scoped whitelisted method; the server decides which
//   organisation the user belongs to, the browser never sends a party.
// * State-changing calls carry an Idempotency-Key, so a double click or a retry after a
//   network error cannot create a second order, ASN or quotation.
// * The DOM is built with createElement/textContent only (no HTML strings), so record
//   values can never inject markup.

const LANG = document.documentElement.lang || navigator.language || "en";

// UI strings go through Frappe's global __() so `bench generate-pot-file` and the page's
// translation dictionary (automotive_portals.portal) pick them up.
const __ = (text, replace) => (window.__ ? window.__(text, replace) : text);

// ---------------------------------------------------------------------------------------
// API

export class ApiError extends Error {
	constructor(message, status) {
		super(message);
		this.status = status;
	}
}

function serverMessage(body, fallback) {
	try {
		if (body && body._server_messages) {
			const messages = JSON.parse(body._server_messages).map((m) => {
				const parsed = typeof m === "string" ? JSON.parse(m) : m;
				return stripTags(parsed.message || "");
			});
			if (messages.filter(Boolean).length) return messages.filter(Boolean).join(" ");
		}
		if (body && body.exception) return stripTags(String(body.exception).split(":").slice(1).join(":").trim());
	} catch (e) {
		// fall through
	}
	return fallback;
}

function stripTags(text) {
	// DOMParser documents are inert (no script execution, no resource loads)
	const doc = new DOMParser().parseFromString(String(text), "text/html");
	return doc.body ? doc.body.textContent || "" : "";
}

async function request(method, options) {
	let response;
	try {
		response = await fetch(`/api/method/${method}${options.query || ""}`, {
			credentials: "same-origin",
			...options,
		});
	} catch (e) {
		throw new ApiError(__("The server could not be reached. Check your connection and retry."), 0);
	}
	let body = null;
	try {
		body = await response.json();
	} catch (e) {
		body = null;
	}
	if (!response.ok) {
		if (response.status === 403) {
			throw new ApiError(serverMessage(body, __("You do not have access to this record.")), 403);
		}
		if (response.status === 401) {
			window.location.href = `/login?redirect-to=${encodeURIComponent(window.location.pathname)}`;
		}
		throw new ApiError(serverMessage(body, __("The request failed ({0}).", [response.status])), response.status);
	}
	return body ? body.message : null;
}

export const api = {
	get(method, args = {}) {
		const params = new URLSearchParams();
		for (const [key, value] of Object.entries(args)) {
			if (value === undefined || value === null) continue;
			params.set(key, typeof value === "object" ? JSON.stringify(value) : String(value));
		}
		const query = params.toString() ? `?${params}` : "";
		return request(method, { method: "GET", query, headers: { Accept: "application/json" } });
	},
	post(method, args = {}, idempotencyKey = newKey()) {
		return request(method, {
			method: "POST",
			headers: {
				Accept: "application/json",
				"Content-Type": "application/json",
				"X-Frappe-CSRF-Token": (window.frappe && window.frappe.csrf_token) || "",
				"Idempotency-Key": idempotencyKey,
			},
			body: JSON.stringify(args),
		});
	},
};

export function newKey() {
	if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
	return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// ---------------------------------------------------------------------------------------
// DOM

export function h(tag, attrs = {}, ...children) {
	const el = document.createElement(tag);
	for (const [key, value] of Object.entries(attrs || {})) {
		if (value === undefined || value === null || value === false) continue;
		if (key === "class") el.className = value;
		else if (key === "dataset") Object.assign(el.dataset, value);
		else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
		else if (value === true) el.setAttribute(key, "");
		else el.setAttribute(key, String(value));
	}
	append(el, children);
	return el;
}

function append(el, children) {
	for (const child of children.flat(Infinity)) {
		if (child === undefined || child === null || child === false) continue;
		el.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
	}
}

export function clear(el) {
	while (el.firstChild) el.removeChild(el.firstChild);
	return el;
}

let uid = 0;
export function nextId(prefix = "f") {
	uid += 1;
	return `${prefix}-${uid}`;
}

// ---------------------------------------------------------------------------------------
// Formatting

export function fmtDate(value) {
	if (!value) return "";
	const date = new Date(`${String(value).slice(0, 10)}T00:00:00`);
	if (Number.isNaN(date.getTime())) return String(value);
	return date.toLocaleDateString(LANG, { year: "numeric", month: "short", day: "numeric" });
}

export function fmtNumber(value, digits = 2) {
	const number = Number(value || 0);
	return number.toLocaleString(LANG, { maximumFractionDigits: digits });
}

export function fmtMoney(value, currency) {
	const number = Number(value || 0);
	if (!currency) return fmtNumber(number);
	try {
		return number.toLocaleString(LANG, { style: "currency", currency });
	} catch (e) {
		return `${fmtNumber(number)} ${currency}`;
	}
}

export function status(text) {
	const tone = {
		Accepted: "good", Approved: "good", Received: "good", Completed: "good", Paid: "good", Delivered: "good",
		Resolved: "good", Acknowledged: "good", Applied: "good", Released: "good",
		Rejected: "bad", Cancelled: "bad", Overdue: "bad", Open: "warn", Awaiting: "warn", Pending: "warn",
		Published: "warn", "In Transit": "info", Shipped: "info", Dispatched: "info", Submitted: "info",
	}[text] || "neutral";
	return h("span", { class: `hp-status hp-status-${tone}` }, __(text || ""));
}

// ---------------------------------------------------------------------------------------
// Components

export function notice(region, message, kind = "error") {
	clear(region);
	if (!message) return;
	region.appendChild(h("div", { class: `hp-notice hp-notice-${kind}` }, message));
	region.focus();
}

export function table({ caption, columns, rows, empty, onOpen }) {
	if (!rows || !rows.length) {
		return h("p", { class: "hp-empty" }, empty || __("Nothing to show yet."));
	}
	const head = h("tr", {}, columns.map((c) => h("th", { scope: "col", class: c.numeric ? "hp-num" : "" }, c.label)));
	const body = rows.map((row) =>
		h("tr", {}, columns.map((c, i) => {
			const content = c.render ? c.render(row) : row[c.field];
			if (i === 0 && onOpen) {
				return h("th", { scope: "row" },
					h("button", { type: "button", class: "hp-link", onclick: () => onOpen(row) }, content));
			}
			return h("td", { class: c.numeric ? "hp-num" : "" }, content);
		})),
	);
	return h("div", { class: "hp-table-wrap", role: "region", "aria-label": caption, tabindex: "0" },
		h("table", { class: "hp-table" }, h("caption", {}, caption), h("thead", {}, head), h("tbody", {}, body)));
}

export function field({ label, name, type = "text", value, required, options, min, step, help, attrs = {} }) {
	const id = nextId(name);
	let control;
	if (type === "select") {
		control = h("select", { id, name, required, ...attrs },
			(options || []).map((o) => {
				const opt = typeof o === "object" ? o : { value: o, label: o };
				return h("option", { value: opt.value, selected: String(opt.value) === String(value ?? "") }, opt.label);
			}));
	} else if (type === "textarea") {
		control = h("textarea", { id, name, required, rows: 3, ...attrs }, value || "");
	} else {
		control = h("input", { id, name, type, value: value ?? "", required, min, step, ...attrs });
	}
	const helpId = help ? `${id}-help` : null;
	if (helpId) control.setAttribute("aria-describedby", helpId);
	return h("div", { class: "hp-field" }, h("label", { for: id }, label, required ? h("span", { "aria-hidden": "true" }, " *") : null),
		control, help ? h("small", { id: helpId }, help) : null);
}

export function form({ legend, fields, submitLabel, onSubmit, region }) {
	const status_ = h("div", { class: "hp-form-status", role: "status", "aria-live": "polite", tabindex: "-1" });
	const submit = h("button", { type: "submit", class: "hp-button hp-primary" }, submitLabel);
	// one key per form instance: resubmitting after an error replays instead of duplicating
	let key = newKey();
	const el = h("form", { class: "hp-form", novalidate: true },
		h("fieldset", {}, h("legend", {}, legend), fields, h("div", { class: "hp-actions" }, submit)), status_);
	el.addEventListener("submit", async (event) => {
		event.preventDefault();
		if (!el.checkValidity()) {
			el.reportValidity();
			return;
		}
		submit.disabled = true;
		notice(status_, __("Sending…"), "info");
		try {
			const result = await onSubmit(new FormData(el), key, el);
			key = newKey();
			notice(status_, null);
			if (region) notice(region, result || __("Saved."), "success");
		} catch (e) {
			notice(status_, e.message || String(e), "error");
		} finally {
			submit.disabled = false;
		}
	});
	return el;
}

// counters the server left out (not for the user's portal role) are not shown
export function counters(items) {
	return h("ul", { class: "hp-counters" }, items.filter((item) => item.value !== undefined).map((item) =>
		h("li", {}, h("button", { type: "button", class: "hp-counter", onclick: item.onClick },
			h("span", { class: "hp-counter-value" }, fmtNumber(item.value, 0)),
			h("span", { class: "hp-counter-label" }, item.label)))));
}

// Tabs following the WAI-ARIA tabs pattern (arrow keys move between tabs).
export function shell(root, { title, party, tabs: all, can }) {
	// a tab that needs a capability the portal role lacks is left out (the server refuses it anyway)
	const tabs = all.filter((tab) => !tab.needs || !can || can.has(tab.needs));
	const alertRegion = h("div", { class: "hp-alert", role: "alert", tabindex: "-1" });
	const panel = h("section", { class: "hp-panel", role: "tabpanel", tabindex: "0" });
	const buttons = tabs.map((tab, index) =>
		h("button", {
			type: "button", role: "tab", id: `hp-tab-${tab.key}`, "aria-controls": "hp-panel",
			"aria-selected": "false", tabindex: "-1", dataset: { index },
			onclick: () => select(tab.key),
		}, tab.label));
	const list = h("div", { class: "hp-tabs", role: "tablist", "aria-label": title }, buttons);
	list.addEventListener("keydown", (event) => {
		const current = buttons.findIndex((b) => b.getAttribute("aria-selected") === "true");
		let next = null;
		if (event.key === "ArrowRight") next = (current + 1) % buttons.length;
		if (event.key === "ArrowLeft") next = (current - 1 + buttons.length) % buttons.length;
		if (event.key === "Home") next = 0;
		if (event.key === "End") next = buttons.length - 1;
		if (next !== null) {
			event.preventDefault();
			select(tabs[next].key);
			buttons[next].focus();
		}
	});
	panel.id = "hp-panel";
	clear(root).appendChild(h("div", { class: "hp-portal" },
		h("header", { class: "hp-header" }, h("h1", {}, title), h("p", { class: "hp-party" }, party)),
		list, alertRegion, panel));

	// a navigation clears the alert region unless it follows a success the user should see
	async function select(key, params = {}, { keepNotice = false } = {}) {
		const index = tabs.findIndex((tab) => tab.key === key);
		const tab = tabs[index >= 0 ? index : 0];
		buttons.forEach((b, i) => {
			const active = tabs[i].key === tab.key;
			b.setAttribute("aria-selected", active ? "true" : "false");
			b.tabIndex = active ? 0 : -1;
		});
		panel.setAttribute("aria-labelledby", `hp-tab-${tab.key}`);
		clear(panel).appendChild(h("p", { class: "hp-loading" }, __("Loading…")));
		if (!keepNotice) notice(alertRegion, null);
		if (window.location.hash.slice(1) !== tab.key) history.replaceState(null, "", `#${tab.key}`);
		try {
			const content = await tab.render({ ...params, go: select, alert: alertRegion });
			clear(panel).appendChild(content);
		} catch (e) {
			clear(panel);
			notice(alertRegion, e.message || String(e), "error");
		}
	}

	// a changed fragment (edited URL, in-page link, back/forward) opens that tab
	window.addEventListener("hashchange", () => {
		const key = window.location.hash.slice(1);
		const active = buttons.find((b) => b.getAttribute("aria-selected") === "true");
		if (key && tabs.some((tab) => tab.key === key) && active?.id !== `hp-tab-${key}`) select(key);
	});
	select(window.location.hash.slice(1) || tabs[0].key);
	return select;
}

export function section(title, ...children) {
	return h("section", { class: "hp-section" }, h("h2", {}, title), children);
}

export function back(go, key, label) {
	return h("button", { type: "button", class: "hp-button hp-secondary", onclick: () => go(key) }, label);
}
