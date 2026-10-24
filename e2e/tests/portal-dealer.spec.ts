// Dealer portal (ERP-08, ERP-10): a dealer's staff raise support requests and follow their
// orders, allocated vehicles, deliveries and invoices, on a desktop or a tablet.
import { expect, test } from "@playwright/test";
import { getList, openDesk } from "../support/frappe";
import { as } from "../support/personas";

test("a dealer raises a support request and follows its business in the portal", async ({ browser }) => {
	const { context, page } = await as(browser, "dealer");
	await page.goto("/dealer-portal#support");
	const subject = `Navigation unit restarts (${Date.now()})`;
	await page.getByRole("textbox", { name: "Subject" }).fill(subject);
	await page.getByRole("textbox", { name: "Description" }).fill("Customer reports the head unit restarting at idle.");
	await page.getByRole("button", { name: "Send request" }).click();
	const notice = page.locator(".hp-alert .hp-notice-success");
	await expect(notice).toContainText("Support request");
	const issue = /Support request (\S+) created/.exec((await notice.textContent()) ?? "")?.[1];
	await expect(page.getByRole("table", { name: "Your support requests" })).toContainText(subject);

	// the dealer liaison sees it on the desk, raised for this dealer
	const liaison = await as(browser, "dealer_liaison");
	await openDesk(liaison.page);
	const [desk] = await getList(liaison.page, "Issue", { name: issue }, ["name", "subject", "customer"]);
	expect(desk).toMatchObject({ subject });

	// every other tab renders the dealer's own records without errors
	for (const tab of ["Overview", "Orders", "Allocated Vehicles", "Deliveries", "Invoices"]) {
		await page.getByRole("tab", { name: tab }).click();
		await expect(page.getByRole("tab", { name: tab })).toHaveAttribute("aria-selected", "true");
		await expect(page.locator(".hp-loading")).toHaveCount(0);
		await expect(page.locator(".hp-alert .hp-notice-error")).toHaveCount(0);
	}
	for (const ctx of [context, liaison.context]) await ctx.close();
});
