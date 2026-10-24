// Partner portal steps shared by the journeys.
import { expect, type Page } from "@playwright/test";
import { type Variant } from "./fixtures";

/** Configure a variant in the dealer portal, add it to the order and place the order;
 * returns the Dealer Order name from the confirmation. */
export async function placeDealerOrder(page: Page, variant: Variant, reference: string, deliveryInDays = 30) {
	await page.goto("/dealer-portal#configure");
	// role names leave out the aria-hidden "required" asterisk that label text includes
	await page.getByRole("combobox", { name: "Model", exact: true }).selectOption(variant.model);
	for (const [attribute, value] of Object.entries(variant.selections)) {
		await page.getByRole("combobox", { name: attribute, exact: true }).selectOption(value);
	}
	await page.getByRole("button", { name: "Check configuration" }).click();
	await expect(page.locator(".hp-config-result .hp-notice-success")).toContainText(variant.variant);
	await page.getByRole("button", { name: "Add to order" }).click();
	await expect(page.locator("table", { hasText: variant.variant })).toBeVisible();
	await page.getByLabel("Your order reference").fill(reference);
	const day = new Date(Date.now() + deliveryInDays * 86_400_000).toISOString().slice(0, 10);
	await page.getByLabel("Requested delivery date").fill(day);
	await page.getByRole("button", { name: "Place order" }).click();
	const notice = page.locator(".hp-alert .hp-notice-success");
	await expect(notice).toContainText("placed");
	const order = /Order (\S+) placed/.exec((await notice.textContent()) ?? "")?.[1];
	expect(order, "the confirmation names the order").toBeTruthy();
	return order as string;
}
