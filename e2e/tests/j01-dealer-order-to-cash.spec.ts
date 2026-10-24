// J-01: dealer order -> configuration -> allocation -> manufacturing -> inspection ->
// shipment -> invoice. Built to order: the order gets its own work order (and VIN) before
// the build, and the released vehicle is allocated to that order. Each step is done by
// the person who does it, in the portal or the desk; the end state is a traceable
// vehicle, a shipment, a receivable and the matching ledger entries.
import { expect, test } from "@playwright/test";
import {
	answerPrompt, call, clickAction, getDoc, getList, insertDoc, mapDoc, openDesk, openForm, saveDoc, submitDoc,
} from "../support/frappe";
import { fixtures } from "../support/fixtures";
import { as, userOf } from "../support/personas";
import { stages } from "../support/journey";
import { placeDealerOrder } from "../support/portal";

test("J-01 a dealer's order is built, inspected, shipped and invoiced", async ({ browser }) => {
	test.setTimeout(480_000); // nine people, two channels, one vehicle
	const fx = fixtures();
	const plan = fx.j01;
	test.skip(!plan, "no build-to-order variant could be staged on this dataset");
	if (!plan) return;
	const stage = stages("J-01");
	const auditor = await as(browser, "auditor"); // reads across functions to check each step
	await openDesk(auditor.page);

	stage("1. the dealer configures the vehicle and orders it in the portal");
	const dealer = await as(browser, "dealer");
	const order = await placeDealerOrder(dealer.page, plan, `J01-${Date.now()}`);
	expect((await getDoc(auditor.page, "Dealer Order", order)).status).toBe("Submitted");

	stage("2. the dealer liaison accepts it: a sales order at the dealer price");
	const liaison = await as(browser, "dealer_liaison");
	await openForm(liaison.page, "Dealer Order", order);
	await clickAction(liaison.page, "Accept");
	await expect.poll(async () => (await getDoc(auditor.page, "Dealer Order", order)).sales_order).toBeTruthy();
	const so = await getDoc(auditor.page, "Sales Order", (await getDoc(auditor.page, "Dealer Order", order)).sales_order);
	expect(so.items[0].item_code).toBe(plan.variant);

	stage("3. the planner gives the order a build slot: a work order for its line, VIN reserved");
	const planner = await as(browser, "planner");
	await openDesk(planner.page);
	const draft = await insertDoc(planner.page, {
		doctype: "Work Order", production_item: plan.variant, qty: 1, company: fx.company,
		planned_start_date: new Date(Date.now() + 60_000).toISOString().slice(0, 19).replace("T", " "),
		source_warehouse: plan.line_side, wip_warehouse: plan.wip_warehouse, fg_warehouse: plan.fg_warehouse,
		skip_transfer: 1, use_multi_level_bom: 0, automotive_plant: plan.plant, automotive_production_line: plan.line,
		sales_order: so.name, sales_order_item: so.items[0].name,
		automotive_configuration: so.items[0].automotive_configuration,
	});
	for (const row of draft.required_items) row.source_warehouse = plan.line_side;
	const wo = await saveDoc(planner.page, draft);
	expect(wo.bom_no).toBe(plan.bom);
	await submitDoc(planner.page, "Work Order", wo.name);
	const [planned] = await getList(auditor.page, "Vehicle Unit", { work_order: wo.name }, ["name", "sales_order",
		"production_status"]);
	expect(planned).toMatchObject({ sales_order: so.name, production_status: "Planned" });

	stage("4. the line reports the unit built (MES call, retried with the same key: one build)");
	const supervisor = await as(browser, "supervisor");
	await openDesk(supervisor.page);
	const key = `j01-${wo.name}-${Date.now()}`;
	const method = "automotive_manufacturing.mes.production.report_unit_built";
	const built = await call(supervisor.page, method, { work_order: wo.name }, { "Idempotency-Key": key });
	const retried = await call(supervisor.page, method, { work_order: wo.name }, { "Idempotency-Key": key });
	expect(retried.stock_entry).toBe(built.stock_entry);
	expect(built.serial_no).toBe(planned.name);
	expect((await getDoc(auditor.page, "Work Order", wo.name)).produced_qty).toBe(1);
	const vin = planned.name;

	stage("5. final inspection by the line's inspector releases the vehicle");
	const inspector = await as(browser, "inspector");
	await openDesk(inspector.page);
	const qi = await insertDoc(inspector.page, {
		doctype: "Quality Inspection", inspection_type: "In Process", automotive_inspection_stage: "Final",
		reference_type: "Stock Entry", reference_name: built.stock_entry, item_code: plan.variant, item_serial_no: vin,
		sample_size: 1, inspected_by: userOf("inspector"), status: "Accepted", manual_inspection: 1, company: fx.company,
	});
	await submitDoc(inspector.page, "Quality Inspection", qi.name);
	await expect.poll(async () => (await getDoc(auditor.page, "Vehicle Unit", vin)).quality_status).toBe("Released");

	stage("6. allocation: the vehicle built for the order is reserved for it (serial-level reservation)");
	await openDesk(liaison.page);
	await call(liaison.page, "automotive_supply_chain.dealer_operations.allocation.run_allocation", { company: fx.company });
	const unit = await getDoc(auditor.page, "Vehicle Unit", vin);
	expect(unit).toMatchObject({ inventory_status: "Reserved", sales_order: so.name, dealer: fx.dealer });
	const allocation = await getDoc(auditor.page, "Vehicle Allocation", unit.automotive_allocation);
	expect((await getDoc(auditor.page, "Stock Reservation Entry", allocation.stock_reservation_entry)).docstatus).toBe(1);

	stage("7. logistics ships it: delivery note, car carrier booking, manifest, proof of delivery");
	const logistics = await as(browser, "logistics");
	await openDesk(logistics.page);
	const dnDraft = await mapDoc(logistics.page, "erpnext.selling.doctype.sales_order.sales_order.make_delivery_note", so.name);
	for (const row of dnDraft.items) Object.assign(row, { use_serial_batch_fields: 1, serial_no: vin, warehouse: unit.current_warehouse });
	const dn = await insertDoc(logistics.page, dnDraft);
	await submitDoc(logistics.page, "Delivery Note", dn.name);
	expect((await getDoc(auditor.page, "Vehicle Unit", vin)).inventory_status).toBe("Delivered");
	const [carrier] = await getList(logistics.page, "Supplier", { supplier_group: "Carriers", disabled: 0 }, ["name"], 1,
		"name asc");
	const today = new Date().toISOString().slice(0, 10);
	const later = new Date(Date.now() + 2 * 86_400_000).toISOString().slice(0, 10);
	const booking = await insertDoc(logistics.page, {
		doctype: "Transport Booking", carrier: carrier.name, company: fx.company, plant: plan.plant, direction: "Outbound",
		vehicle_type: "Car Carrier", pickup_window_start: `${today} 00:00:00`, pickup_window_end: `${today} 23:00:00`,
		delivery_window_end: `${later} 18:00:00`, destination: fx.dealer, capacity_units: 8,
	});
	const manifest = await insertDoc(logistics.page, {
		doctype: "Shipment Manifest", company: fx.company, plant: plan.plant, transport_booking: booking.name,
		carrier: carrier.name, departure: new Date(Date.now() + 60_000).toISOString().slice(0, 19).replace("T", " "),
		seal_no: `J01-${Date.now() % 1_000_000}`, items: [{ delivery_note: dn.name }],
	});
	await submitDoc(logistics.page, "Shipment Manifest", manifest.name);
	expect((await getDoc(auditor.page, "Vehicle Unit", vin)).shipment_status).toBe("Dispatched");
	await openForm(logistics.page, "Shipment Manifest", manifest.name);
	await clickAction(logistics.page, "Record Proof Of Delivery");
	await answerPrompt(logistics.page, { received_by: "Dealer yard manager" });
	await expect.poll(async () => (await getDoc(auditor.page, "Shipment Manifest", manifest.name)).delivery_status)
		.toBe("Delivered");

	stage("8. the dealer confirms the vehicle arrived in good condition: warranty starts");
	await dealer.page.getByRole("tab", { name: "Deliveries" }).click();
	await dealer.page.locator("tr", { hasText: dn.name }).getByRole("button", { name: "Record acceptance" }).click();
	await dealer.page.getByRole("button", { name: "Confirm" }).click();
	await expect(dealer.page.locator(".hp-alert .hp-notice-success")).toContainText(dn.name);
	await expect.poll(async () => (await getDoc(auditor.page, "Vehicle Unit", vin)).shipment_status).toBe("Accepted");
	const accepted = await getDoc(auditor.page, "Vehicle Unit", vin);
	expect(accepted.warranty_start_date && accepted.warranty_end_date).toBeTruthy();

	stage("9. accounts invoice the delivery: a receivable for the dealer and the revenue");
	const accounts = await as(browser, "ap_clerk");
	await openDesk(accounts.page);
	const si = await insertDoc(accounts.page,
		await mapDoc(accounts.page, "erpnext.stock.doctype.delivery_note.delivery_note.make_sales_invoice", dn.name));
	await submitDoc(accounts.page, "Sales Invoice", si.name);
	await expect.poll(async () => (await getDoc(auditor.page, "Vehicle Unit", vin)).invoice_status).toBe("Invoiced");

	stage("end state: the vehicle traces from order to invoice and the ledgers agree");
	const invoice = await getDoc(auditor.page, "Sales Invoice", si.name);
	expect(invoice.customer).toBe(fx.dealer);
	const gl = await getList(auditor.page, "GL Entry", { voucher_no: si.name, is_cancelled: 0 },
		["account", "debit", "credit", "party"], 50);
	const debit = gl.reduce((s, e) => s + e.debit, 0);
	const credit = gl.reduce((s, e) => s + e.credit, 0);
	expect(Math.abs(debit - credit)).toBeLessThan(0.01);
	const receivable = gl.filter((e) => e.party === fx.dealer).reduce((s, e) => s + e.debit - e.credit, 0);
	expect(receivable).toBeCloseTo(invoice.grand_total, 2);
	const stockGl = await getList(auditor.page, "GL Entry", { voucher_no: dn.name, is_cancelled: 0 }, ["debit", "credit"], 50);
	expect(stockGl.length, "the delivery posts cost of sales against stock").toBeGreaterThan(0);
	const events = (await getList(auditor.page, "Audit Event", { reference_name: allocation.name }, ["event_type"], 20))
		.map((e) => e.event_type);
	expect(events).toContain("allocation.created");
	for (const ctx of [auditor, dealer, liaison, planner, supervisor, inspector, logistics, accounts]) await ctx.context.close();
});
