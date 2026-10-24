// J-10: a partner message fails, is delivered again (at least once), retried after the
// cause is fixed, and reconciled: exactly one business effect. The partner's system is
// played by a plain HTTPS client that signs Hinode EDI like a real EDI provider would.
import { expect, test } from "@playwright/test";
import { answerPrompt, call, clickAction, getDoc, getList, openDesk, openForm } from "../support/frappe";
import { fixtures } from "../support/fixtures";
import { stages } from "../support/journey";
import { deliver } from "../support/partner";
import { as } from "../support/personas";

test("J-10 a failed partner message is retried once its cause is fixed, with one business effect", async ({
	browser, playwright, baseURL,
}) => {
	const edi = fixtures().j10;
	test.skip(!edi, "no EDI supplier with an open order line on this dataset");
	if (!edi) return;
	const stage = stages("J-10");
	const partner = await playwright.request.newContext({ baseURL });
	const it = await as(browser, "it_operator");
	await openDesk(it.page);
	const buyer = await as(browser, "buyer");
	const po = edi.purchase_order;
	const asnNo = `E2E-${Date.now()}`;
	const jst = (offsetHours = 0) => new Date(Date.now() + (9 + offsetHours) * 3_600_000).toISOString().slice(0, 19);
	const envelope = {
		message_id: `ASN.${asnNo}`, message_type: "asn", schema_version: "v1", sent_at: jst(),
		data: {
			supplier_asn_no: asnNo, ship_date: jst(), expected_arrival: jst(24), carrier: "Partner Express",
			tracking_no: `TRK-${asnNo}`, packages: 1,
			lines: [{ purchase_order: po, po_item: edi.po_item, item_code: edi.item_code, qty: edi.qty,
				supplier_lot: edi.has_batch_no ? `LOT-${asnNo}` : null, serial_nos: [] }],
		},
	};
	const status = async (name: string) => (await getDoc(it.page, "Integration Inbound Message", name)).status;

	stage("1. the buyer puts the order on hold while a price query is open");
	await openForm(buyer.page, "Purchase Order", po);
	await clickAction(buyer.page, "Hold", "Status");
	await answerPrompt(buyer.page, { reason_for_hold: "Price query open with the supplier." });
	await expect.poll(async () => (await getDoc(buyer.page, "Purchase Order", po)).status).toBe("On Hold");

	stage("2. the supplier's system sends its shipment notice: accepted, then processing fails");
	const first = await deliver(partner, edi, envelope);
	expect(first.status).toBe(202);
	expect(first.body.status).toBe("accepted");
	const inbound = first.body.inbound_id;
	await expect.poll(() => status(inbound), { timeout: 60_000 }).toBe("Failed");
	expect((await getDoc(it.page, "Integration Inbound Message", inbound)).last_error).toMatch(/on hold/);

	stage("3. the partner delivers it again (at least once): recognised, stored once");
	const again = await deliver(partner, edi, envelope);
	expect(again.status).toBe(200);
	expect(again.body).toMatchObject({ status: "duplicate", inbound_id: inbound });

	stage("4. the buyer settles the query and resumes the order");
	await openForm(buyer.page, "Purchase Order", po);
	await clickAction(buyer.page, "Resume", "Status");
	await expect.poll(async () => (await getDoc(buyer.page, "Purchase Order", po)).status).not.toBe("On Hold");

	stage("5. the retry: the queue's own if it came first, else the IT operator replays it");
	let replayed = false;
	if ((await status(inbound)) !== "Processed") {
		await openForm(it.page, "Integration Inbound Message", inbound);
		await clickAction(it.page, "Replay");
		replayed = true;
	}
	await expect.poll(() => status(inbound), { timeout: 60_000 }).toBe("Processed");
	const processed = await getDoc(it.page, "Integration Inbound Message", inbound);
	expect(processed.result_doctype).toBe("Advance Shipping Notice");

	stage("6. a late redelivery returns the original result; exactly one shipment notice exists");
	const late = await deliver(partner, edi, envelope);
	expect(late.body).toMatchObject({ status: "duplicate", inbound_id: inbound, processing_status: "Processed",
		result_name: processed.result_name });
	const notices = await getList(buyer.page, "Advance Shipping Notice", { supplier_asn_no: asnNo, docstatus: ["<", 2] });
	expect(notices.map((n) => n.name)).toEqual([processed.result_name]);
	const asn = await getDoc(buyer.page, "Advance Shipping Notice", processed.result_name);
	expect(asn).toMatchObject({ supplier: edi.supplier, docstatus: 1 });
	expect(asn.items).toHaveLength(1);

	stage("7. reconciliation holds and the failure, retry and result are on record");
	expect((await call(it.page, "automotive_core.integration_hub.inbound.reconciliation")).ok).toBe(true);
	const events = (await getList(it.page, "Audit Event", { reference_name: inbound }, ["event_type"], 20))
		.map((e) => e.event_type);
	if (replayed) expect(events).toContain("integration.inbound.replayed");
	expect(processed.attempts).toBeGreaterThanOrEqual(2);
	await partner.dispose();
	for (const ctx of [it, buyer]) await ctx.context.close();
});
