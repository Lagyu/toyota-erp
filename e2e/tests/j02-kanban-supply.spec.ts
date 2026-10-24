// J-02: line consumption -> Kanban signal -> supplier call-off -> ASN -> receipt ->
// replenishment, each step by the person who does it, through the desk and the portal.
import { expect, test } from "@playwright/test";
import { call, clickAction, getDoc, getList, openDesk, openForm } from "../support/frappe";
import { as, userOf } from "../support/personas";

test("J-02 pull replenishment from the line to the supplier and back", async ({ browser }) => {
	// the JIT supplier of the e2e supplier user, one of its loops and a full card
	const auditor = await as(browser, "auditor");
	await openDesk(auditor.page);
	const supplier = (await getList<any>(auditor.page, "Partner User Access",
		{ user: userOf("supplier"), status: "Active" }, ["supplier"]))[0].supplier;
	const loops = await getList<any>(auditor.page, "Kanban Loop", { supplier, status: "Active" },
		["name", "item_code", "line_side_warehouse"], 50, "name asc");
	expect(loops.length, `${supplier} has Kanban loops`).toBeGreaterThan(0);
	let card: any = null;
	let loop: any = null;
	for (const candidate of loops) {
		const cards = await getList<any>(auditor.page, "Kanban Card", { kanban_loop: candidate.name, status: "Full" }, ["name"]);
		if (cards.length) {
			card = cards[0];
			loop = candidate;
			break;
		}
	}
	expect(card, "a full card on one of the supplier's loops").toBeTruthy();
	const clerk = await as(browser, "warehouse"); // stock figures are the warehouse's to read
	await openDesk(clerk.page);
	const binBefore = (await getList<any>(clerk.page, "Bin", { item_code: loop.item_code,
		warehouse: loop.line_side_warehouse }, ["actual_qty"]))[0]?.actual_qty ?? 0;

	// 1. the line empties a container: the supervisor scans the card
	const supervisor = await as(browser, "supervisor");
	await openForm(supervisor.page, "Kanban Card", card.name);
	await clickAction(supervisor.page, "Signal (Card Emptied)");
	await expect.poll(async () => (await getDoc(auditor.page, "Kanban Card", card.name)).status).toBe("Signaled");
	const signal = (await getList<any>(auditor.page, "Kanban Signal", { kanban_card: card.name },
		["name", "material_request", "purchase_order"], 1))[0];
	expect(signal?.material_request, "purchasing is asked at once").toBeTruthy();

	// 2. the buyer releases the call-off against the blanket order (no new approval)
	const buyer = await as(browser, "buyer");
	await openForm(buyer.page, "Kanban Signal", signal.name);
	await clickAction(buyer.page, "Release Call-Offs Now");
	await expect.poll(async () => (await getDoc<any>(auditor.page, "Kanban Signal", signal.name)).purchase_order)
		.toBeTruthy();
	const po = (await getDoc<any>(auditor.page, "Kanban Signal", signal.name)).purchase_order;
	expect((await getDoc<any>(auditor.page, "Purchase Order", po)).automotive_release_type).toBe("Blanket Call-Off");

	// 3. the supplier sees the order in the portal, confirms it and announces the shipment
	const supplierUser = await as(browser, "supplier");
	const portal = supplierUser.page;
	await portal.goto("/supplier-portal#orders");
	await portal.getByRole("button", { name: po, exact: true }).click();
	await expect(portal.getByRole("heading", { name: `Order ${po}` })).toBeVisible();
	await portal.getByRole("button", { name: "Send acknowledgement" }).click();
	await expect(portal.locator(".hp-notice-success")).toContainText("Acknowledgement");
	await portal.getByRole("tab", { name: "Shipments (ASN)" }).click();
	await portal.getByRole("button", { name: "Announce a shipment" }).click();
	// the call-off may carry other cards of the same supplier: ship every line of it
	const asnNo = `E2E-${Date.now()}`;
	const rows = portal.locator("tbody tr", { hasText: po });
	await expect(rows.first()).toBeVisible();
	for (let i = 0; i < (await rows.count()); i++) {
		const line = rows.nth(i);
		await line.locator('input[type="checkbox"]').check();
		const lot = line.locator('input[name^="lot-"]');
		if (await lot.count()) await lot.fill(`LOT-${asnNo}`);
	}
	await portal.getByLabel("Your ASN number").fill(asnNo);
	await portal.getByRole("button", { name: "Send ASN" }).click();
	await expect(portal.locator(".hp-notice-success")).toContainText("announced");
	const asn = (await getList<any>(auditor.page, "Advance Shipping Notice", { supplier_asn_no: asnNo }, ["name"], 1))[0].name;

	// 4. receiving: the clerk turns the ASN into a receipt and submits it
	await openForm(clerk.page, "Advance Shipping Notice", asn);
	await clickAction(clerk.page, "Receive (Purchase Receipt)");
	await expect(clerk.page).toHaveURL(/purchase-receipt\//);
	const receipt = decodeURIComponent(clerk.page.url().split("/purchase-receipt/")[1].split(/[?#]/)[0]);
	await call(clerk.page, "frappe.client.submit", { doc: await getDoc(clerk.page, "Purchase Receipt", receipt) });

	// 5. replenished: card full again, signal closed, stock at line side
	await expect.poll(async () => (await getDoc(auditor.page, "Advance Shipping Notice", asn)).status).toBe("Received");
	await expect.poll(async () => (await getDoc(auditor.page, "Kanban Signal", signal.name)).status).toBe("Received");
	await expect.poll(async () => (await getDoc(auditor.page, "Kanban Card", card.name)).status).toBe("Full");
	const binAfter = (await getList<any>(clerk.page, "Bin", { item_code: loop.item_code,
		warehouse: loop.line_side_warehouse }, ["actual_qty"]))[0].actual_qty;
	expect(binAfter).toBeGreaterThan(binBefore);
	for (const ctx of [auditor, supervisor, buyer, supplierUser, clerk]) await ctx.context.close();
});
