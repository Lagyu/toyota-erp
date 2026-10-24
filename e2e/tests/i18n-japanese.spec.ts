// ERP-12.3: the Japanese user interface. A planner whose language is Japanese works in
// Japanese across the desk itself, a standard ERPNext document and a Hinode document,
// including the messages the desk shows. Frappe and ERPNext ship no Japanese catalogue;
// automotive_core's covers the upstream strings of these screens.
import { expect, test } from "@playwright/test";
import { openDesk } from "../support/frappe";
import { as } from "../support/personas";

test("a Japanese-speaking planner works in Japanese: desk, ERPNext and Hinode screens", async ({ browser }) => {
	const { context, page } = await as(browser, "planner_ja");
	await openDesk(page);
	expect(await page.evaluate(() => (window as any).frappe.boot.lang)).toBe("ja");

	// a standard ERPNext list
	await openDesk(page, "/desk/work-order");
	await expect(page.locator(".page-title")).toContainText("作業指示");

	// a Hinode document: form title, field labels, the save action and the message for a missing value
	await openDesk(page, "/desk/planning-run/new");
	await expect(page.locator(".page-title")).toContainText("計画実行");
	await expect(page.locator(".frappe-control[data-fieldname='plant'] .control-label")).toContainText("工場");
	await expect(page.locator(".frappe-control[data-fieldname='parameter_set'] .control-label"))
		.toContainText("パラメータセットのバージョン");
	const save = page.locator(".page-head .primary-action").first();
	await expect(save).toContainText("保存");
	await save.click();
	await expect(page.locator(".modal.show .modal-title")).toContainText("未入力のフィールド");
	await context.close();
});
