// Supplier portal (ERP-06, ERP-10): a supplier's own staff quote on requests for quotation
// and acknowledge delivery schedules from the portal, on a desktop or a tablet.
import { expect, test } from "@playwright/test";
import { call, getDoc, getList, insertDoc, openDesk, submitDoc } from "../support/frappe";
import { as, userOf } from "../support/personas";

const day = (offset = 0) => new Date(Date.now() + 9 * 3_600_000 + offset * 86_400_000).toISOString().slice(0, 10);

/** The persona's supplier and a part it delivers (looked up by the auditor: test setup only). */
async function suppliedItem(browser: import("@playwright/test").Browser) {
	const { context, page } = await as(browser, "auditor");
	await openDesk(page);
	const [access] = await getList(page, "Partner User Access", { user: userOf("supplier"), status: "Active" }, ["supplier"]);
	const [loop] = await getList(page, "Kanban Loop", { supplier: access.supplier, status: "Active" },
		["item_code", "plant", "line_side_warehouse"], 1, "name asc");
	await context.close();
	return { supplier: access.supplier, ...loop };
}

test("a supplier quotes on a request for quotation in the portal", async ({ browser }) => {
	const { supplier, item_code, plant, line_side_warehouse } = await suppliedItem(browser);
	const buyer = await as(browser, "buyer");
	await openDesk(buyer.page);
	const company = (await getDoc(buyer.page, "Plant", plant)).company;
	const uom = (await getDoc(buyer.page, "Item", item_code)).stock_uom;
	const rfq = await insertDoc(buyer.page, {
		doctype: "Request for Quotation", company, transaction_date: day(), schedule_date: day(21),
		message_for_supplier: "Please quote your best price for next quarter's volume.",
		suppliers: [{ supplier, send_email: 0 }],
		items: [{ item_code, qty: 120, schedule_date: day(21), warehouse: line_side_warehouse, uom, conversion_factor: 1 }],
	});
	await submitDoc(buyer.page, "Request for Quotation", rfq.name);

	const { context, page } = await as(browser, "supplier");
	await page.goto("/supplier-portal#rfqs");
	await page.getByRole("button", { name: rfq.name, exact: true }).click();
	await expect(page.getByRole("heading", { name: `Request ${rfq.name}` })).toBeVisible();
	await page.getByRole("spinbutton", { name: `Unit price for ${item_code}` }).fill("1234.5");
	await page.getByLabel("Valid till").fill(day(45));
	await page.getByRole("spinbutton", { name: "Lead time (days)" }).fill("5");
	await page.getByRole("button", { name: "Submit quotation" }).click();
	const notice = page.locator(".hp-alert .hp-notice-success");
	await expect(notice).toContainText("Quotation");
	await expect(page.getByText("You have quoted on this request.")).toBeVisible();

	const quotes = await getList(buyer.page, "Supplier Quotation",
		[["Supplier Quotation Item", "request_for_quotation", "=", rfq.name]], ["name", "supplier", "docstatus"]);
	expect(quotes).toHaveLength(1);
	expect(quotes[0].supplier).toBe(supplier);
	const quote = await getDoc(buyer.page, "Supplier Quotation", quotes[0].name);
	expect(quote.items[0]).toMatchObject({ item_code, qty: 120, rate: 1234.5 });
	await context.close();
	await buyer.context.close();
});

test("a supplier acknowledges the delivery schedule published to it", async ({ browser }) => {
	const { supplier, item_code, plant, line_side_warehouse } = await suppliedItem(browser);
	const planner = await as(browser, "planner");
	await openDesk(planner.page);
	const company = (await getDoc(planner.page, "Plant", plant)).company;
	// a requirement for the supplier's part (as planning would raise it)
	const request = await insertDoc(planner.page, {
		doctype: "Material Request", material_request_type: "Purchase", company, schedule_date: day(9),
		items: [{ item_code, qty: 40, schedule_date: day(9), warehouse: line_side_warehouse }],
	});
	await submitDoc(planner.page, "Material Request", request.name);

	const buyer = await as(browser, "buyer");
	await openDesk(buyer.page);
	const schedule = await call(buyer.page, "automotive_supply_chain.supplier_collaboration.schedules.generate",
		{ supplier, plant });
	await submitDoc(buyer.page, "Supplier Delivery Schedule", schedule);
	expect((await getDoc(buyer.page, "Supplier Delivery Schedule", schedule)).status).toBe("Published");

	const { context, page } = await as(browser, "supplier");
	await page.goto("/supplier-portal#schedules");
	await page.getByRole("button", { name: schedule, exact: true }).click();
	await expect(page.getByRole("table", { name: "Schedule lines" })).toContainText(item_code);
	await page.getByRole("button", { name: "Acknowledge schedule" }).click();
	await expect(page.locator(".hp-alert .hp-notice-success")).toContainText(schedule);
	await expect.poll(async () => (await getDoc(buyer.page, "Supplier Delivery Schedule", schedule)).status)
		.toBe("Acknowledged");
	for (const ctx of [context, planner.context, buyer.context]) await ctx.close();
});
