// Helpers that act through the running desk in the browser (the user's own session,
// CSRF token and permissions), never through a privileged back door.
import { expect, type Page } from "@playwright/test";

export async function openDesk(page: Page, route = "/desk") {
	await page.goto(route);
	await page.waitForFunction(() => (window as any).frappe?.boot?.user, null, { timeout: 60_000 });
}

/** POST /api/method/<method> as the signed-in user (session cookie + CSRF token, plus any
 * extra headers such as Idempotency-Key); returns `message` or throws with the server's
 * error text. */
export async function call<T = any>(page: Page, method: string, args: Record<string, unknown> = {},
		headers: Record<string, string> = {}): Promise<T> {
	const result = await page.evaluate(
		async ({ method, args, headers }) => {
			const frappe = (window as any).frappe;
			const res = await fetch(`/api/method/${method}`, {
				method: "POST",
				credentials: "same-origin",
				headers: {
					Accept: "application/json",
					"Content-Type": "application/json",
					"X-Frappe-CSRF-Token": frappe?.csrf_token ?? "",
					...headers,
				},
				body: JSON.stringify(args),
			});
			const body = await res.json().catch(() => ({}));
			if (res.ok) return { ok: true, message: body.message };
			let text = body.exception || body.exc_type || "";
			try {
				text = JSON.parse(body._server_messages || "[]").map((m: string) => JSON.parse(m).message).join(" ") || text;
			} catch {
				/* keep exc_type */
			}
			return { ok: false, error: `${method}: HTTP ${res.status} ${text}` };
		},
		{ method, args, headers }
	);
	if (!result.ok) throw new Error(result.error);
	return result.message as T;
}

export async function getDoc<T = any>(page: Page, doctype: string, name: string): Promise<T> {
	return call<T>(page, "frappe.client.get", { doctype, name });
}

export async function getList<T = any>(page: Page, doctype: string, filters: unknown, fields = ["name"], limit = 20,
		order_by = "creation desc"): Promise<T[]> {
	return call<T[]>(page, "frappe.client.get_list", { doctype, filters, fields, limit_page_length: limit, order_by });
}

/** Insert as the signed-in user (the same server path as saving a new form). */
export async function insertDoc<T = any>(page: Page, doc: Record<string, unknown>): Promise<T> {
	return call<T>(page, "frappe.client.insert", { doc });
}

export async function saveDoc<T = any>(page: Page, doc: Record<string, unknown>): Promise<T> {
	return call<T>(page, "frappe.client.save", { doc });
}

/** Submit a saved document as the signed-in user (the form's Submit button). */
export async function submitDoc<T = any>(page: Page, doctype: string, name: string): Promise<T> {
	return call<T>(page, "frappe.client.submit", { doc: await getDoc(page, doctype, name) });
}

/** Run a whitelisted ERPNext mapper (e.g. Sales Order -> Delivery Note) and return the new, unsaved doc. */
export async function mapDoc<T = any>(page: Page, method: string, source_name: string): Promise<T> {
	return call<T>(page, method, { source_name });
}

export async function openForm(page: Page, doctype: string, name: string) {
	const slug = doctype.toLowerCase().replace(/ /g, "-");
	await openDesk(page, `/desk/${slug}/${encodeURIComponent(name)}`);
	await expect(page.locator(".page-title")).toBeVisible();
}

/** Click a custom form button (optionally inside a button group). */
export async function clickAction(page: Page, label: string, group?: string) {
	if (group) {
		await page.getByRole("button", { name: group }).click();
		await page.getByRole("menuitem", { name: label }).or(page.locator(".dropdown-item", { hasText: label })).first().click();
	} else {
		await page.locator(".page-actions, .form-inner-toolbar, .custom-actions").getByRole("button", { name: label }).first().click();
	}
}

/** Fill and confirm a frappe.prompt dialog. */
export async function answerPrompt(page: Page, values: Record<string, string>, confirm?: string) {
	const dialog = page.locator(".modal.show .modal-dialog").last();
	await expect(dialog).toBeVisible();
	for (const [fieldname, value] of Object.entries(values)) {
		const input = dialog.locator(`[data-fieldname="${fieldname}"] textarea, [data-fieldname="${fieldname}"] input`).first();
		await input.fill(value);
	}
	// Frappe v16 marks the dialog's primary action .btn-modal-primary
	await dialog.locator(".btn-modal-primary", confirm ? { hasText: confirm } : {}).first().click();
	await expect(dialog).toBeHidden();
}

/** Ask for approval from the form's Approval menu (the governed document stays a draft). */
export async function requestApproval(page: Page, doctype: string, name: string) {
	await openForm(page, doctype, name);
	await clickAction(page, "Request Approval", "Approval");
	await expect(page.getByText(/Awaiting approval/).first()).toBeVisible();
}

/** Approve the current step from the form's Approval menu. */
export async function approve(page: Page, doctype: string, name: string, comment = "Reviewed: in order.") {
	await openForm(page, doctype, name);
	await clickAction(page, "Approve", "Approval");
	await answerPrompt(page, { comment }, "Approve");
}
