// J-06: supplier invoice -> PO/receipt match -> exception resolution -> finance approval ->
// settlement. The payable and the bank/payment audit chain must reconcile.
import { expect, test } from "@playwright/test";
import {
	answerPrompt, approve, call, clickAction, getDoc, getList, insertDoc, mapDoc, openDesk, openForm, requestApproval,
	saveDoc, submitDoc,
} from "../support/frappe";
import { fixtures } from "../support/fixtures";
import { stages } from "../support/journey";
import { as } from "../support/personas";

test("J-06 a supplier invoice is matched, corrected, approved by two people and paid", async ({ browser }) => {
	const fx = fixtures().j06;
	test.skip(!fx, "no uninvoiced receipt from a supplier with a verified bank account on this dataset");
	if (!fx) return;
	const stage = stages("J-06");
	const today = new Date(Date.now() + 9 * 3_600_000).toISOString().slice(0, 10);
	const auditor = await as(browser, "auditor");
	await openDesk(auditor.page);
	const ap = await as(browser, "ap_clerk");
	await openDesk(ap.page);

	stage("1. the supplier bills 6% above the order price; AP enters the invoice against the receipt");
	const draft = await mapDoc(ap.page, "erpnext.stock.doctype.purchase_receipt.purchase_receipt.make_purchase_invoice",
		fx.purchase_receipt);
	const orderRate = draft.items[0].rate;
	draft.items[0].rate = Math.round(orderRate * 1.06 * 100) / 100;
	Object.assign(draft, { bill_no: `SUP-INV-${Date.now()}`, bill_date: today });
	const pi = await insertDoc(ap.page, draft);
	expect(pi.automotive_match_status).toBe("Exceptions Open");
	const [exception] = await getList(ap.page, "Invoice Match Exception", { purchase_invoice: pi.name },
		["name", "exception_type", "status"]);
	expect(exception).toMatchObject({ exception_type: "Price Variance", status: "Open" });

	stage("2. an invoice with an open exception cannot be posted");
	await expect(submitDoc(ap.page, "Purchase Invoice", pi.name)).rejects.toThrow();

	stage("3. procurement rejects the variance: the supplier must bill the agreed price");
	const procurement = await as(browser, "procurement_manager");
	await openForm(procurement.page, "Invoice Match Exception", exception.name);
	await clickAction(procurement.page, "Reject");
	await answerPrompt(procurement.page, { note: "The price increase was not agreed; bill the order price." });
	await expect.poll(async () => (await getDoc(ap.page, "Invoice Match Exception", exception.name)).status)
		.toBe("Rejected");

	stage("4. AP corrects the invoice to the order price: matched and posted");
	const current = await getDoc(ap.page, "Purchase Invoice", pi.name);
	current.items[0].rate = orderRate;
	expect((await saveDoc(ap.page, current)).automotive_match_status).toBe("Matched");
	await submitDoc(ap.page, "Purchase Invoice", pi.name);
	expect((await getDoc(ap.page, "Invoice Match Exception", exception.name)).status).toBe("Corrected");
	const payable = (await getDoc(ap.page, "Purchase Invoice", pi.name)).outstanding_amount;
	expect(payable).toBeGreaterThan(0);

	stage("5. AP prepares the wire to the verified bank account; releasing it needs approval");
	const wire = await call(ap.page, "erpnext.accounts.doctype.payment_entry.payment_entry.get_payment_entry",
		{ dt: "Purchase Invoice", dn: pi.name });
	const pe = await insertDoc(ap.page, { ...wire, mode_of_payment: "Wire Transfer", paid_from: fx.paid_from,
		party_bank_account: fx.bank_account, reference_no: `WIRE-${Date.now()}`, reference_date: today });
	await expect(submitDoc(ap.page, "Payment Entry", pe.name)).rejects.toThrow(/approv/i);
	await requestApproval(ap.page, "Payment Entry", pe.name);

	stage("6. two different treasury approvers release it; nobody approves twice");
	const first = await as(browser, "treasury");
	await approve(first.page, "Payment Entry", pe.name, "Supplier and bank account verified.");
	const [request] = await getList(first.page, "Approval Request", { reference_name: pe.name }, ["name", "status"]);
	await expect(call(first.page, "automotive_core.governance.approvals.decide",
		{ request: request.name, decision: "Approve" })).rejects.toThrow();
	const second = await as(browser, "treasury_2");
	await approve(second.page, "Payment Entry", pe.name, "Second approval: amount and invoice checked.");
	await expect.poll(async () => (await getDoc(first.page, "Approval Request", request.name)).status).toBe("Approved");

	stage("7. AP posts the payment: the payable is settled and every ledger agrees");
	await submitDoc(ap.page, "Payment Entry", pe.name);
	expect((await getDoc(auditor.page, "Purchase Invoice", pi.name)).outstanding_amount).toBe(0);
	const ple = await getList(auditor.page, "Payment Ledger Entry", { against_voucher_no: pi.name, delinked: 0 },
		["voucher_no", "amount"], 50);
	expect(ple.reduce((sum, e) => sum + e.amount, 0)).toBeCloseTo(0, 2);
	const gl = await getList(auditor.page, "GL Entry", { voucher_no: pe.name, is_cancelled: 0 },
		["account", "debit", "credit"], 20);
	expect(gl.find((e) => e.account === fx.paid_from)?.credit).toBeCloseTo(payable, 2);
	const events = (await getList(auditor.page, "Audit Event", { reference_name: pe.name }, ["event_type"], 50))
		.map((e) => e.event_type);
	expect(events).toEqual(expect.arrayContaining(["approval.requested", "approval.approve", "document.submitted"]));
	for (const ctx of [auditor, ap, procurement, first, second]) await ctx.context.close();
});
