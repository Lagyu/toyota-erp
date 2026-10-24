// Role surfaces (ERP-10, ERP-12): every persona signs in as a real dataset user and sees
// the workspaces of its function and not those of others; partner users land on their
// portal, cannot use the desk and cannot open the other partner portal.
import { expect, test } from "@playwright/test";
import { openDesk } from "../support/frappe";
import { as, type Persona } from "../support/personas";

const EXPECTED: Record<string, { sees: string[]; hidden: string[] }> = {
	buyer: { sees: ["Supplier Collaboration", "Procurement Controls", "Governance"], hidden: ["Finance Controls", "Management"] },
	ap_clerk: { sees: ["Procurement Controls", "Finance Controls"], hidden: ["Product Engineering", "Shop Floor"] },
	treasury: { sees: ["Finance Controls", "Governance"], hidden: ["Supplier Collaboration", "Shop Floor"] },
	controller: { sees: ["Finance Controls", "Management", "Intercompany"], hidden: ["Shop Floor"] },
	executive: { sees: ["Management"], hidden: ["Automotive Setup"] },
	supervisor: { sees: ["Shop Floor", "Production Planning"], hidden: ["Finance Controls", "Procurement Controls"] },
	operator: { sees: ["Shop Floor"], hidden: ["Supplier Collaboration", "Finance Controls", "Management"] },
	inspector: { sees: ["Quality Control", "Traceability"], hidden: ["Finance Controls"] },
	engineer: { sees: ["Product Engineering"], hidden: ["Finance Controls", "Procurement Controls"] },
	warehouse: { sees: ["Logistics", "Supplier Collaboration"], hidden: ["Finance Controls", "Management"] },
	dealer_liaison: { sees: ["Dealer Operations"], hidden: ["Finance Controls", "Shop Floor"] },
	auditor: { sees: ["Governance", "Management", "Finance Controls"], hidden: [] },
	it_operator: { sees: ["Integration Hub", "Automotive Setup"], hidden: [] },
};

for (const [persona, expectation] of Object.entries(EXPECTED)) {
	test(`${persona} sees the workspaces of its function`, async ({ browser }) => {
		const { context, page } = await as(browser, persona as Persona);
		await openDesk(page);
		const allowed: string[] = await page.evaluate(() =>
			((window as any).frappe.boot.allowed_workspaces || []).map((w: any) => w.name)
		);
		for (const name of expectation.sees) expect(allowed, `${persona} should see ${name}`).toContain(name);
		for (const name of expectation.hidden) expect(allowed, `${persona} must not see ${name}`).not.toContain(name);
		await context.close();
	});
}

test("a workspace shows its KPI cards to the function's users", async ({ browser }) => {
	const { context, page } = await as(browser, "buyer");
	await openDesk(page, "/desk/supplier-collaboration");
	await expect(page.locator(".widget-title, .number-card-title", { hasText: "Supplier On-Time Delivery" })).toBeVisible();
	await expect(page.locator(".widget-title, .number-card-title", { hasText: "Open Receipt Discrepancies" })).toBeVisible();
	await context.close();
});

test("supplier users land on the supplier portal and cannot use the desk", async ({ browser }) => {
	const { context, page } = await as(browser, "supplier");
	await page.goto("/supplier-portal");
	await expect(page.getByRole("heading", { level: 1, name: "Supplier Portal" })).toBeVisible();
	await expect(page.getByRole("tab", { name: "Orders" })).toBeVisible();
	const desk = await page.goto("/desk");
	expect(page.url()).not.toContain("/desk/");
	expect([200, 403, 302, 301]).toContain(desk?.status() ?? 200);
	const dealerPortal = await page.goto("/dealer-portal");
	expect(dealerPortal?.status()).toBe(403);
	await context.close();
});

test("dealer users land on the dealer portal and cannot open the supplier portal", async ({ browser }) => {
	const { context, page } = await as(browser, "dealer");
	await page.goto("/dealer-portal");
	await expect(page.getByRole("heading", { level: 1, name: "Dealer Portal" })).toBeVisible();
	await expect(page.getByRole("tab", { name: "Configure & Order" })).toBeVisible();
	const supplierPortal = await page.goto("/supplier-portal");
	expect(supplierPortal?.status()).toBe(403);
	await context.close();
});

test("portal tabs are keyboard operable", async ({ browser }) => {
	const { context, page } = await as(browser, "dealer");
	await page.goto("/dealer-portal#overview");
	const overview = page.getByRole("tab", { name: "Overview" });
	await overview.focus();
	await page.keyboard.press("ArrowRight");
	await expect(page.getByRole("tab", { name: "Configure & Order" })).toHaveAttribute("aria-selected", "true");
	await page.keyboard.press("End");
	await expect(page.getByRole("tab", { name: "Support" })).toHaveAttribute("aria-selected", "true");
	await context.close();
});
