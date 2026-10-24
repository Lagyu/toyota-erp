// Helpers that act through the running desk in the browser (the user's own session,
// CSRF token and permissions), never through a privileged back door.
import { expect, type Page } from "@playwright/test";

export async function openDesk(page: Page, route = "/desk") {
	await page.goto(route);
	await page.waitForFunction(() => (window as any).frappe?.boot?.user, null, { timeout: 60_000 });
}

/** frappe.call as the signed-in user; returns r.message or throws the server message. */
export async function call<T = any>(page: Page, method: string, args: Record<string, unknown> = {}): Promise<T> {
	return page.evaluate(
		async ({ method, args }) => {
			const frappe = (window as any).frappe;
			const r = await frappe.call({ method, args, freeze: false, error_handlers: {} });
			return r.message;
		},
		{ method, args }
	);
}

export async function getDoc<T = any>(page: Page, doctype: string, name: string): Promise<T> {
	return call<T>(page, "frappe.client.get", { doctype, name });
}

export async function getList<T = any>(page: Page, doctype: string, filters: unknown, fields = ["name"], limit = 20,
		order_by = "creation desc"): Promise<T[]> {
	return call<T[]>(page, "frappe.client.get_list", { doctype, filters, fields, limit_page_length: limit, order_by });
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
	await dialog.locator(".btn-primary", confirm ? { hasText: confirm } : {}).first().click();
	await expect(dialog).toBeHidden();
}
