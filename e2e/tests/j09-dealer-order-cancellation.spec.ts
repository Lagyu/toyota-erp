// J-09: a dealer cancels an order after a vehicle was allocated to it. The change goes
// through the portal; permissions, the stock reservation, inventory valuation and the
// audit history must stay correct.
import { expect, test } from "@playwright/test";
import { call, clickAction, getDoc, getList, openDesk, openForm } from "../support/frappe";
import { fixtures } from "../support/fixtures";
import { stages } from "../support/journey";
import { as } from "../support/personas";
import { placeDealerOrder } from "../support/portal";

test("J-09 cancelling an allocated order releases the vehicle and keeps the books intact", async ({ browser }) => {
	const fx = fixtures();
	const variant = fx.j09;
	test.skip(!variant, "no orderable variant with a free vehicle in stock on this dataset");
	if (!variant) return;
	const stage = stages("J-09");
	const auditor = await as(browser, "auditor");
	await openDesk(auditor.page);

	stage("1. the dealer orders a variant that is in stock; the liaison accepts it");
	const dealer = await as(browser, "dealer");
	const order = await placeDealerOrder(dealer.page, variant, `J09-${Date.now()}`, 14);
	const liaison = await as(browser, "dealer_liaison");
	await openForm(liaison.page, "Dealer Order", order);
	await clickAction(liaison.page, "Accept");
	await expect.poll(async () => (await getDoc(auditor.page, "Dealer Order", order)).sales_order).toBeTruthy();
	const so = (await getDoc(auditor.page, "Dealer Order", order)).sales_order;

	stage("2. allocation reserves a released vehicle from stock for it");
	await call(liaison.page, "automotive_supply_chain.dealer_operations.allocation.run_allocation", { company: fx.company });
	const [allocation] = await getList(auditor.page, "Vehicle Allocation", { sales_order: so, status: "Allocated" },
		["name", "vehicle_unit", "stock_reservation_entry", "warehouse"]);
	expect(allocation, "a vehicle is allocated to the order").toBeTruthy();
	expect(await getDoc(auditor.page, "Vehicle Unit", allocation.vehicle_unit)).toMatchObject({
		inventory_status: "Reserved", dealer: fx.dealer, sales_order: so });
	expect((await getDoc(auditor.page, "Stock Reservation Entry", allocation.stock_reservation_entry)).docstatus).toBe(1);
	const bin = { item_code: variant.variant, warehouse: allocation.warehouse };
	const [before] = await getList(auditor.page, "Bin", bin, ["actual_qty", "stock_value", "reserved_stock"]);

	stage("3. a dealer cannot touch another dealer's order");
	const [foreign] = await getList(auditor.page, "Dealer Order", { dealer: ["!=", fx.dealer], status: "Accepted" },
		["name"], 1);
	if (foreign) {
		await expect(call(dealer.page, "automotive_portals.api.dealer.request_change", {
			dealer_order: foreign.name, change_type: "Cancel Order", reason: "Not ours to cancel" },
			{ "Idempotency-Key": `j09-foreign-${Date.now()}` })).rejects.toThrow(/HTTP 403/);
	}

	stage("4. the dealer cancels the order in the portal");
	await dealer.page.getByRole("tab", { name: "Orders" }).click();
	await dealer.page.getByRole("button", { name: order, exact: true }).click();
	await expect(dealer.page.getByRole("heading", { name: `Order ${order}` })).toBeVisible();
	await dealer.page.getByRole("combobox", { name: "Change", exact: true }).selectOption("Cancel Order");
	await dealer.page.getByRole("textbox", { name: "Reason", exact: true }).fill("The end customer withdrew the order.");
	await dealer.page.getByRole("button", { name: "Send change request" }).click();
	await expect(dealer.page.locator(".hp-alert .hp-notice-success")).toContainText("Change");

	stage("5. order, reservation and allocation are unwound; the vehicle is free again");
	const [change] = await getList(auditor.page, "Dealer Order Change", { dealer_order: order }, ["name", "status"]);
	expect(change.status).toBe("Applied");
	expect((await getDoc(auditor.page, "Sales Order", so)).docstatus).toBe(2);
	expect((await getDoc(auditor.page, "Stock Reservation Entry", allocation.stock_reservation_entry)).docstatus).toBe(2);
	expect((await getDoc(auditor.page, "Vehicle Allocation", allocation.name)).status).toBe("Released");
	const unit = await getDoc(auditor.page, "Vehicle Unit", allocation.vehicle_unit);
	expect(unit.inventory_status).toBe("In Stock");
	expect(unit.dealer ?? null).toBeNull();
	expect(unit.automotive_allocation ?? null).toBeNull();

	stage("6. inventory value never moved; the audit trail records each step");
	const [after] = await getList(auditor.page, "Bin", bin, ["actual_qty", "stock_value", "reserved_stock"]);
	expect(after.actual_qty).toBe(before.actual_qty);
	expect(after.stock_value).toBeCloseTo(before.stock_value, 2);
	expect(after.reserved_stock).toBe(before.reserved_stock - 1);
	const events = async (name: string) =>
		(await getList(auditor.page, "Audit Event", { reference_name: name }, ["event_type"], 50)).map((e) => e.event_type);
	expect(await events(allocation.name)).toEqual(expect.arrayContaining(["allocation.created", "allocation.released"]));
	expect(await events(change.name)).toContain("document.submitted");
	for (const ctx of [auditor, dealer, liaison]) await ctx.context.close();
});
