// J-07: Kyushu ships powertrains to Aichi at the agreed transfer price, and Aichi exports to
// North America in dollars. Each company's own people book their side (order, delivery,
// receipt, invoices); both companies end with matching commercial and stock events and
// balancing receivable and payable.
import { expect, test } from "@playwright/test";
import { approve, call, getDoc, getList, insertDoc, mapDoc, openDesk, requestApproval, submitDoc } from "../support/frappe";
import { fixtures } from "../support/fixtures";
import { stages } from "../support/journey";
import { as } from "../support/personas";

test("J-07 an intercompany powertrain shipment is booked on both sides and balances", async ({ browser }) => {
	const fx = fixtures().j07;
	test.skip(!fx, "no powertrain in Kyushu stock with an agreed transfer price on this dataset");
	if (!fx) return;
	const stage = stages("J-07");
	const day = (offset = 0) => new Date(Date.now() + 9 * 3_600_000 + offset * 86_400_000).toISOString().slice(0, 10);
	const auditor = await as(browser, "auditor");
	await openDesk(auditor.page);
	const stock = async (warehouse: string) =>
		(await getList(auditor.page, "Bin", { item_code: fx.item_code, warehouse }, ["actual_qty"]))[0]?.actual_qty ?? 0;
	const before = { seller: await stock(fx.seller_warehouse), buyer: await stock(fx.buyer_warehouse) };

	stage("1. Aichi's buyer orders from Kyushu; only the agreed transfer price is accepted");
	const buyer = await as(browser, "buyer");
	await openDesk(buyer.page);
	const order = {
		doctype: "Purchase Order", supplier: fx.supplier, company: fx.buyer, buying_price_list: "Intercompany JPY",
		transaction_date: day(), schedule_date: day(3),
		items: [{ item_code: fx.item_code, qty: 1, rate: fx.rate * 0.97, warehouse: fx.buyer_warehouse, schedule_date: day(3) }],
	};
	await expect(insertDoc(buyer.page, order)).rejects.toThrow(/transfer price/);
	order.items[0].rate = fx.rate;
	const po = await insertDoc(buyer.page, order);
	if ((await call(buyer.page, "automotive_core.governance.approvals.get_status",
			{ doctype: "Purchase Order", name: po.name })).policy) {
		await requestApproval(buyer.page, "Purchase Order", po.name);
		const procurement = await as(browser, "procurement_manager");
		await approve(procurement.page, "Purchase Order", po.name);
		await procurement.context.close();
	}
	await submitDoc(buyer.page, "Purchase Order", po.name);

	stage("2. the group sales desk confirms it as Kyushu's sales order (mapped, never retyped)");
	const sales = await as(browser, "sales_manager");
	await openDesk(sales.page);
	const so = await call(sales.page, "automotive_supply_chain.intercompany.handoff.create_seller_order",
		{ purchase_order: po.name, delivery_date: day(2), warehouse: fx.seller_warehouse });
	expect(await call(sales.page, "automotive_supply_chain.intercompany.handoff.create_seller_order",
		{ purchase_order: po.name })).toBe(so); // asking twice does not duplicate it
	await submitDoc(sales.page, "Sales Order", so);
	expect(await getDoc(auditor.page, "Sales Order", so)).toMatchObject({ company: fx.seller,
		inter_company_order_reference: po.name });

	stage("3. Kyushu logistics picks a serialised powertrain and ships it");
	const shipper = await as(browser, "kyushu_logistics");
	await openDesk(shipper.page);
	const [unit] = await getList(shipper.page, "Serial No", { item_code: fx.item_code, warehouse: fx.seller_warehouse,
		status: "Active" }, ["name"], 1, "creation asc");
	const dnDraft = await mapDoc(shipper.page, "erpnext.selling.doctype.sales_order.sales_order.make_delivery_note", so);
	for (const row of dnDraft.items) Object.assign(row, { qty: 1, use_serial_batch_fields: 1, serial_no: unit.name });
	const dn = await insertDoc(shipper.page, dnDraft);
	await submitDoc(shipper.page, "Delivery Note", dn.name);

	stage("4. Aichi receiving books the arrival from the delivery it cannot otherwise see");
	const receiving = await as(browser, "warehouse");
	await openDesk(receiving.page);
	await expect(getDoc(receiving.page, "Delivery Note", dn.name)).rejects.toThrow(/HTTP 403/);
	const pr = await call(receiving.page, "automotive_supply_chain.intercompany.handoff.create_buyer_receipt",
		{ delivery_note: dn.name, warehouse: fx.buyer_warehouse });
	await submitDoc(receiving.page, "Purchase Receipt", pr);

	stage("5. accounts: Kyushu invoices the shipment; Aichi books the mirrored invoice, matched");
	const accounts = await as(browser, "ap_clerk");
	await openDesk(accounts.page);
	const si = await insertDoc(accounts.page,
		await mapDoc(accounts.page, "erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", dn.name));
	await submitDoc(accounts.page, "Sales Invoice", si.name);
	const piDraft = await mapDoc(accounts.page,
		"erpnext.accounts.doctype.sales_invoice.sales_invoice.make_inter_company_purchase_invoice", si.name);
	const pi = await insertDoc(accounts.page, { ...piDraft, bill_no: si.name, bill_date: day() });
	expect(pi.automotive_match_status).toBe("Matched");
	await submitDoc(accounts.page, "Purchase Invoice", pi.name);

	stage("6. both companies agree: stock moved once, receivable equals payable");
	expect(await stock(fx.seller_warehouse)).toBe(before.seller - 1);
	expect(await stock(fx.buyer_warehouse)).toBe(before.buyer + 1);
	expect((await getDoc(auditor.page, "Serial No", unit.name)).warehouse).toBe(fx.buyer_warehouse);
	const controller = await as(browser, "controller");
	await openDesk(controller.page);
	const recon = await call(controller.page, "automotive_supply_chain.intercompany.transfer_pricing.reconciliation",
		{ seller: fx.seller, buyer: fx.buyer });
	expect(recon.balanced, JSON.stringify(recon)).toBe(true);
	const [siGl, piGl] = [await getDoc(auditor.page, "Sales Invoice", si.name), await getDoc(auditor.page, "Purchase Invoice", pi.name)];
	expect(piGl.grand_total).toBeCloseTo(siGl.grand_total, 2);
	for (const ctx of [auditor, buyer, sales, shipper, receiving, accounts, controller]) await ctx.context.close();
});

test("J-07 an export to North America is booked in dollars on both sides and balances", async ({ browser }) => {
	const fx = fixtures().j07na;
	test.skip(!fx, "no internal supplier, controller or dollar price list on this dataset");
	if (!fx) return;
	const stage = stages("J-07 NA");
	const day = (offset = 0) => new Date(Date.now() + 9 * 3_600_000 + offset * 86_400_000).toISOString().slice(0, 10);
	const auditor = await as(browser, "auditor");
	await openDesk(auditor.page);
	const stock = async (warehouse: string) =>
		(await getList(auditor.page, "Bin", { item_code: fx.item_code, warehouse }, ["actual_qty"]))[0]?.actual_qty ?? 0;
	const before = { seller: await stock(fx.seller_warehouse), buyer: await stock(fx.buyer_warehouse) };

	stage("1. North America orders from Aichi in dollars; only the agreed transfer price is accepted");
	const buyer = await as(browser, "buyer");
	await openDesk(buyer.page);
	const order = {
		doctype: "Purchase Order", supplier: fx.supplier, company: fx.buyer, currency: fx.currency,
		buying_price_list: fx.price_list, transaction_date: day(), schedule_date: day(28),
		items: [{ item_code: fx.item_code, qty: fx.qty, rate: fx.rate * 0.97, warehouse: fx.buyer_warehouse,
			schedule_date: day(28) }],
	};
	await expect(insertDoc(buyer.page, order)).rejects.toThrow(/transfer price/);
	order.items[0].rate = fx.rate;
	const po = await insertDoc(buyer.page, order);
	if ((await call(buyer.page, "automotive_core.governance.approvals.get_status",
			{ doctype: "Purchase Order", name: po.name })).policy) {
		await requestApproval(buyer.page, "Purchase Order", po.name);
		const procurement = await as(browser, "procurement_manager");
		await approve(procurement.page, "Purchase Order", po.name);
		await procurement.context.close();
	}
	await submitDoc(buyer.page, "Purchase Order", po.name);

	stage("2. Aichi's sales desk confirms it as Aichi's sales order, in the same dollars");
	const sales = await as(browser, "sales_manager");
	await openDesk(sales.page);
	const so = await call(sales.page, "automotive_supply_chain.intercompany.handoff.create_seller_order",
		{ purchase_order: po.name, delivery_date: day(7), warehouse: fx.seller_warehouse });
	await submitDoc(sales.page, "Sales Order", so);
	const confirmed = await getDoc(auditor.page, "Sales Order", so);
	expect(confirmed).toMatchObject({ company: fx.seller, currency: fx.currency, inter_company_order_reference: po.name });
	expect(confirmed.items.map((r: { item_code: string; qty: number; rate: number }) => [r.item_code, r.qty, r.rate]))
		.toEqual([[fx.item_code, fx.qty, fx.rate]]);

	stage("3. Aichi logistics ships the kits");
	const shipper = await as(browser, "logistics");
	await openDesk(shipper.page);
	const dn = await insertDoc(shipper.page,
		await mapDoc(shipper.page, "erpnext.selling.doctype.sales_order.sales_order.make_delivery_note", so));
	await submitDoc(shipper.page, "Delivery Note", dn.name);

	stage("4. North America receiving books the arrival");
	const receiving = await as(browser, "na_warehouse");
	await openDesk(receiving.page);
	const pr = await call(receiving.page, "automotive_supply_chain.intercompany.handoff.create_buyer_receipt",
		{ delivery_note: dn.name, warehouse: fx.buyer_warehouse });
	await submitDoc(receiving.page, "Purchase Receipt", pr);

	stage("5. accounts: Aichi invoices in dollars; North America books the mirrored invoice, matched");
	const accounts = await as(browser, "ap_clerk");
	await openDesk(accounts.page);
	const si = await insertDoc(accounts.page,
		await mapDoc(accounts.page, "erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", dn.name));
	await submitDoc(accounts.page, "Sales Invoice", si.name);
	const piDraft = await mapDoc(accounts.page,
		"erpnext.accounts.doctype.sales_invoice.sales_invoice.make_inter_company_purchase_invoice", si.name);
	const pi = await insertDoc(accounts.page, { ...piDraft, bill_no: si.name, bill_date: day() });
	expect(pi.automotive_match_status).toBe("Matched");
	await submitDoc(accounts.page, "Purchase Invoice", pi.name);

	stage("6. the same dollars on both sides, Aichi's in yen at the invoice's rate; stock moved once");
	const [sold, bought] = [await getDoc(auditor.page, "Sales Invoice", si.name),
		await getDoc(auditor.page, "Purchase Invoice", pi.name)];
	expect([sold.currency, bought.currency]).toEqual([fx.currency, fx.currency]);
	expect(sold.net_total).toBeCloseTo(fx.qty * fx.rate, 2);
	expect(bought.grand_total).toBeCloseTo(sold.grand_total, 2);
	expect(sold.conversion_rate).toBeGreaterThan(1); // yen per dollar
	expect(Math.abs(sold.base_grand_total - sold.grand_total * sold.conversion_rate)).toBeLessThanOrEqual(1);
	expect(bought.base_grand_total).toBeCloseTo(bought.grand_total, 2); // North America keeps dollars
	expect(await stock(fx.seller_warehouse)).toBe(before.seller - fx.qty);
	expect(await stock(fx.buyer_warehouse)).toBe(before.buyer + fx.qty);
	const controller = await as(browser, "controller");
	await openDesk(controller.page);
	const recon = await call(controller.page, "automotive_supply_chain.intercompany.transfer_pricing.reconciliation",
		{ seller: fx.seller, buyer: fx.buyer });
	expect(recon.balanced, JSON.stringify(recon)).toBe(true);
	for (const ctx of [auditor, buyer, sales, shipper, receiving, accounts, controller]) await ctx.context.close();
});
