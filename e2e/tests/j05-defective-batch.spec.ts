// J-05: defective batch -> quarantine -> andon -> nonconformance -> disposition -> restart.
// No unreleased stock may enter production or shipment while the batch is held.
import { expect, test } from "@playwright/test";
import {
	answerPrompt, approve, call, clickAction, getDoc, getList, insertDoc, openDesk, openForm, requestApproval, saveDoc,
	submitDoc,
} from "../support/frappe";
import { fixtures } from "../support/fixtures";
import { stages } from "../support/journey";
import { as, userOf } from "../support/personas";

test("J-05 a defective batch is segregated, dispositioned, and the line restarts only after release", async ({ browser }) => {
	const fx = fixtures().j05;
	test.skip(!fx, "no supplier batch in use at an Aichi line on this dataset");
	if (!fx) return;
	test.setTimeout(300_000);
	const stage = stages("J-05");
	const auditor = await as(browser, "auditor");
	await openDesk(auditor.page);
	const batchQty = async (warehouse: string) =>
		Number(await call(auditor.page, "erpnext.stock.doctype.batch.batch.get_batch_qty",
			{ batch_no: fx.batch_no, warehouse })) || 0;
	const move = (to: string, qty: number) => ({
		doctype: "Stock Entry", purpose: "Material Transfer", stock_entry_type: "Material Transfer", company: fx.company,
		items: [{ item_code: fx.item_code, qty, s_warehouse: fx.line_side, t_warehouse: to, use_serial_batch_fields: 1,
			batch_no: fx.batch_no }],
	});

	stage("1. incoming re-inspection finds the batch defective: it is held and a case is opened");
	const inspector = await as(browser, "inspector");
	await openDesk(inspector.page);
	const qi = await insertDoc(inspector.page, {
		doctype: "Quality Inspection", inspection_type: "Incoming", automotive_inspection_stage: "Incoming",
		reference_type: "Purchase Receipt", reference_name: fx.purchase_receipt, child_row_reference: fx.receipt_row,
		item_code: fx.item_code, batch_no: fx.batch_no, sample_size: 2, inspected_by: userOf("inspector"),
		status: "Rejected", manual_inspection: 1, company: fx.company,
		remarks: "Housing cracks on 2 of 2 samples taken at the line.",
	});
	await submitDoc(inspector.page, "Quality Inspection", qi.name);
	const inspected = await getDoc(inspector.page, "Quality Inspection", qi.name);
	const batchHold = inspected.automotive_quality_hold;
	const nc = inspected.automotive_nonconformance;
	expect(await getDoc(auditor.page, "Quality Hold", batchHold)).toMatchObject({ hold_scope: "Batch",
		batch_no: fx.batch_no, status: "Active" });

	stage("2. the held batch cannot move anywhere but into segregation; the clerk quarantines it");
	const clerk = await as(browser, "warehouse");
	await openDesk(clerk.page);
	await expect(insertDoc(clerk.page, move(fx.store, 1))).rejects.toThrow(/hold/i);
	const onLine = await batchQty(fx.line_side);
	const segregation = await insertDoc(clerk.page, move(fx.quarantine, onLine));
	await submitDoc(clerk.page, "Stock Entry", segregation.name);
	expect(await batchQty(fx.line_side)).toBe(0);
	expect(await batchQty(fx.quarantine)).toBe(onLine);

	stage("3. the operator stops the line with a quality andon naming the suspect batch");
	const operator = await as(browser, "operator");
	await openDesk(operator.page);
	const andon = await insertDoc(operator.page, {
		doctype: "Andon Event", production_line: fx.line, workstation: fx.workstation, reason_code: "Q-DEFECT",
		stop_type: "Line Stop", item_code: fx.item_code, batch_no: fx.batch_no,
		description: "Cracked housing found while fitting; parts from the suspect batch on the rack.",
	});
	const raised = await getDoc(auditor.page, "Andon Event", andon.name);
	expect(raised.status).toBe("Raised");
	const stopHold = raised.quality_hold;
	expect((await getDoc(auditor.page, "Quality Hold", stopHold)).hold_scope).toBe("Workstation");
	const batchHolds = await getList(auditor.page, "Quality Hold", { batch_no: fx.batch_no, status: "Active" }, ["name"]);
	expect(batchHolds.map((h) => h.name), "one hold per batch, confirmed by the andon").toEqual([batchHold]);

	stage("4. the supervisor responds and records containment; the stop joins the open case");
	const supervisor = await as(browser, "supervisor");
	await openForm(supervisor.page, "Andon Event", andon.name);
	await clickAction(supervisor.page, "Acknowledge");
	await expect.poll(async () => (await getDoc(auditor.page, "Andon Event", andon.name)).status).toBe("Acknowledged");
	await clickAction(supervisor.page, "Record Containment");
	await answerPrompt(supervisor.page, { action: "Rack purged to quarantine; last five units re-checked, no defect." });
	await expect.poll(async () => (await getDoc(auditor.page, "Andon Event", andon.name)).status).toBe("Contained");
	expect((await getDoc(auditor.page, "Andon Event", andon.name)).nonconformance).toBe(nc);

	stage("5. quality returns the batch to the supplier; a second quality manager approves");
	const quality = await as(browser, "quality_manager");
	await openDesk(quality.page);
	const disposition = await insertDoc(quality.page, {
		doctype: "Quality Disposition", quality_hold: batchHold, nonconformance: nc, company: fx.company,
		decision: "Return to Supplier", item_code: fx.item_code, batch_no: fx.batch_no, qty: onLine,
		from_warehouse: fx.quarantine, supplier: fx.supplier,
		justification: "Supplier process defect: cracked housings; full batch returned for credit.",
	});
	await expect(submitDoc(quality.page, "Quality Disposition", disposition.name)).rejects.toThrow(/approv/i);
	await requestApproval(quality.page, "Quality Disposition", disposition.name);
	const checker = await as(browser, "quality_manager_2");
	await approve(checker.page, "Quality Disposition", disposition.name, "Return agreed with the supplier.");
	await submitDoc(quality.page, "Quality Disposition", disposition.name);
	const executed = await getDoc(auditor.page, "Quality Disposition", disposition.name);
	expect((await getDoc(auditor.page, "Purchase Receipt", executed.purchase_return)).is_return).toBe(1);
	expect((await getDoc(auditor.page, "Quality Hold", batchHold)).status).toBe("Dispositioned");
	expect(await batchQty(fx.quarantine)).toBe(0);

	stage("6. root cause and a completed corrective action close the case");
	const capa = await insertDoc(quality.page, {
		doctype: "Quality Action", corrective_preventive: "Corrective", status: "Completed",
		date: new Date(Date.now() + 9 * 3_600_000).toISOString().slice(0, 10),
		resolutions: [{ problem: "Cracked connector housings from the supplier",
			resolution: "Supplier moulding temperature corrected; incoming sampling raised for three lots",
			status: "Completed" }],
	});
	const caseDoc = await getDoc(quality.page, "Non Conformance", nc);
	Object.assign(caseDoc, { automotive_root_cause_method: "5 Why",
		automotive_root_cause: "Supplier moulding temperature drifted below specification",
		corrective_action: caseDoc.corrective_action || "Batch segregated and returned", automotive_capa: capa.name,
		status: "Resolved" });
	await saveDoc(quality.page, caseDoc);

	stage("7. the stop's hold is released by quality and the restart authorised");
	const hold = await getDoc(quality.page, "Quality Hold", stopHold);
	for (const check of hold.release_checks) check.done = 1;
	await saveDoc(quality.page, hold);
	await openForm(quality.page, "Quality Hold", stopHold);
	await clickAction(quality.page, "Release Hold");
	await answerPrompt(quality.page, { note: "Station cleared; suspect stock returned; root cause known." });
	await expect.poll(async () => (await getDoc(auditor.page, "Quality Hold", stopHold)).status).toBe("Released");
	await openForm(quality.page, "Andon Event", andon.name);
	await clickAction(quality.page, "Authorise Restart");
	await answerPrompt(quality.page, { note: "Restart after verification of the first unit." });
	await expect.poll(async () => (await getDoc(auditor.page, "Andon Event", andon.name)).status).toBe("Restarted");
	await openForm(supervisor.page, "Andon Event", andon.name);
	await clickAction(supervisor.page, "Close");
	await expect.poll(async () => (await getDoc(auditor.page, "Andon Event", andon.name)).status).toBe("Closed");

	stage("8. end state: none of the batch reached production after the hold; the record is complete");
	expect(await batchQty(fx.line_side)).toBe(0);
	const stopped = await getDoc(auditor.page, "Andon Event", andon.name);
	expect(stopped.duration_minutes).toBeGreaterThanOrEqual(0);
	expect(stopped.restart_authorized_by).toBe(userOf("quality_manager"));
	expect((await getDoc(auditor.page, "Non Conformance", nc)).status).toBe("Resolved");
	const events = async (doctype: string, name: string) => (await getList(auditor.page, "Audit Event",
		{ reference_doctype: doctype, reference_name: name }, ["event_type"], 50)).map((e) => e.event_type);
	expect(await events("Quality Hold", batchHold)).toEqual(expect.arrayContaining(["quality.hold_raised",
		"quality.hold_confirmed"]));
	expect(await events("Andon Event", andon.name)).toEqual(expect.arrayContaining(["andon.raised", "andon.restarted"]));
	expect(await events("Quality Disposition", disposition.name)).toContain("quality.disposition_executed");
	for (const ctx of [auditor, inspector, clerk, operator, supervisor, quality, checker]) await ctx.context.close();
});
