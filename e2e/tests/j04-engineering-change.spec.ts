// J-04: engineering change request -> impact -> ECO -> approval -> new BOM revision ->
// effective work orders. Orders planned after the effective date use the new revision;
// history stays pinned to the revision it was built on.
import { expect, test } from "@playwright/test";
import {
	approve, call, clickAction, getDoc, getList, insertDoc, openDesk, openForm, requestApproval, submitDoc,
} from "../support/frappe";
import { fixtures } from "../support/fixtures";
import { stages } from "../support/journey";
import { as } from "../support/personas";

test("J-04 an approved engineering change takes effect for new work orders only", async ({ browser }) => {
	const fx = fixtures().j04;
	test.skip(!fx, "no released module BOM with built work orders on this dataset");
	if (!fx) return;
	const stage = stages("J-04");
	const engineer = await as(browser, "engineer");
	await openDesk(engineer.page);

	stage("1. the engineer raises a change request and assesses its impact");
	const ecr = await insertDoc(engineer.page, {
		doctype: "Engineering Change Request", title: `One more ${fx.component} per ${fx.item}`,
		change_reason: "Quality Improvement", priority: "Medium",
		description: `<p>Field feedback: add one ${fx.component} to ${fx.item}.</p>`,
		affected_items: [{ item_code: fx.component, current_bom: fx.bom, proposed_change: `${fx.qty} -> ${fx.new_qty}` }],
	});
	await openForm(engineer.page, "Engineering Change Request", ecr.name);
	await clickAction(engineer.page, "Assess Impact");
	await expect.poll(async () => (await getDoc(engineer.page, "Engineering Change Request", ecr.name)).impact_summary)
		.toContain("BOM");
	await submitDoc(engineer.page, "Engineering Change Request", ecr.name);

	stage("2. the request becomes an ECO; the engineer sets the new quantity and asks for approval");
	await openForm(engineer.page, "Engineering Change Request", ecr.name);
	await clickAction(engineer.page, "Create ECO");
	await engineer.page.waitForURL(/engineering-change-order\/new-/);
	await engineer.page.waitForFunction(() => (window as any).cur_frm?.doc?.changes?.length > 0);
	await engineer.page.evaluate((qty) => {
		const w = window as any;
		const row = w.cur_frm.doc.changes[0];
		return w.frappe.model.set_value(row.doctype, row.name, "qty", qty);
	}, fx.new_qty);
	await engineer.page.keyboard.press("Control+s");
	await engineer.page.waitForURL(/engineering-change-order\/ECO-/);
	const eco = decodeURIComponent(engineer.page.url().split("/engineering-change-order/")[1].split(/[?#]/)[0]);
	expect((await getDoc(engineer.page, "Engineering Change Order", eco)).changes[0]).toMatchObject({
		target_bom: fx.bom, component: fx.component, qty: fx.new_qty });
	await expect(submitDoc(engineer.page, "Engineering Change Order", eco)).rejects.toThrow(/approv/i);
	await requestApproval(engineer.page, "Engineering Change Order", eco);

	stage("3. engineering and quality approve; the change is released");
	const manager = await as(browser, "engineering_manager");
	await approve(manager.page, "Engineering Change Order", eco, "Design reviewed.");
	const quality = await as(browser, "quality_manager");
	await approve(quality.page, "Engineering Change Order", eco, "No quality objection.");
	await submitDoc(manager.page, "Engineering Change Order", eco);
	const released = await getDoc(manager.page, "Engineering Change Order", eco);
	expect(released.implementation_status).toBe("Implemented");
	const revision = released.revisions.find((r: any) => r.old_bom === fx.bom)?.new_bom;
	expect(revision, "a new revision supersedes the BOM").toBeTruthy();
	expect((await getDoc(manager.page, "BOM", revision)).items.find((i: any) => i.item_code === fx.component).qty)
		.toBe(fx.new_qty);
	expect((await getDoc(engineer.page, "Engineering Change Request", ecr.name)).status).toBe("Converted");

	stage("4. planning picks the revision effective on each order's start date");
	const planner = await as(browser, "planner");
	await openDesk(planner.page);
	const history = await getDoc(planner.page, "Work Order", fx.work_order);
	const draft = (start: string) => ({
		doctype: "Work Order", production_item: fx.item, qty: 1, company: history.company, planned_start_date: start,
		source_warehouse: history.source_warehouse, wip_warehouse: history.wip_warehouse,
		fg_warehouse: history.fg_warehouse, skip_transfer: 1, use_multi_level_bom: 0,
		automotive_plant: history.automotive_plant, automotive_production_line: history.automotive_production_line,
	});
	const after = new Date(`${released.effective_date}T00:00:00Z`);
	after.setUTCDate(after.getUTCDate() + 1);
	const later = await insertDoc(planner.page, draft(`${after.toISOString().slice(0, 10)} 08:00:00`));
	const today = new Date(Date.now() + 9 * 3_600_000).toISOString().slice(0, 10);
	const now = await insertDoc(planner.page, draft(`${today} 23:00:00`));
	expect(later.bom_no).toBe(revision);
	expect(now.bom_no).toBe(fx.bom);
	for (const wo of [later, now]) await call(planner.page, "frappe.client.delete", { doctype: "Work Order", name: wo.name });

	stage("5. history stays pinned: built orders keep their revision, which cannot be withdrawn");
	expect((await getDoc(planner.page, "Work Order", fx.work_order)).bom_no).toBe(fx.bom);
	await expect(call(manager.page, "frappe.client.cancel", { doctype: "BOM", name: fx.bom })).rejects.toThrow();
	const auditor = await as(browser, "auditor"); // the audit trail is the auditors' to read
	await openDesk(auditor.page);
	const events = (await getList(auditor.page, "Audit Event", { reference_name: eco }, ["event_type"], 50))
		.map((e) => e.event_type);
	expect(events).toEqual(expect.arrayContaining(["approval.requested", "approval.approve", "document.submitted"]));
	for (const ctx of [engineer, manager, quality, planner, auditor]) await ctx.context.close();
});
