// J-03: demand -> MRP -> shortages -> approved purchase/production proposals. The planner
// runs MRP from the plant's approved parameter set; the run reports its exceptions,
// replays identically from its stored inputs, and its approved proposals become the
// documents purchasing and production act on.
import { expect, test } from "@playwright/test";
import { clickAction, getDoc, getList, openDesk, openForm } from "../support/frappe";
import { stages } from "../support/journey";
import { as } from "../support/personas";

test("J-03 an MRP run turns demand into exceptions and released, actionable proposals", async ({ browser }) => {
	test.setTimeout(480_000);
	const stage = stages("J-03");
	const planner = await as(browser, "planner");
	await openDesk(planner.page);
	const auditor = await as(browser, "auditor");
	await openDesk(auditor.page);
	const runOf = (url: string) => decodeURIComponent(url.split("/planning-run/")[1].split(/[?#]/)[0]);
	const status = async (name: string) => (await getDoc(planner.page, "Planning Run", name)).status;
	// a run is queued and computed by a background worker: wait until it settles, then report
	const settled = async (name: string) => {
		await expect.poll(() => status(name), { timeout: 360_000, intervals: [3_000] })
			.toMatch(/^(Completed|Failed|Cancelled)$/);
		const doc = await getDoc(planner.page, "Planning Run", name);
		expect(doc.status, doc.error ?? "").toBe("Completed");
		return doc;
	};

	stage("1. the planner runs MRP from the plant's approved parameter set");
	const [params] = await getList(planner.page, "Planning Parameter Set", { plant: "AICHI-VA", docstatus: 1 },
		["name"], 1, "creation desc");
	await openForm(planner.page, "Planning Parameter Set", params.name);
	await clickAction(planner.page, "Run Planning");
	await planner.page.waitForURL(/planning-run\/MRP-/);
	const run = runOf(planner.page.url());
	const planned = await settled(run);

	stage("2. exceptions: demand covered, shortages and capacity reported, proposals of both kinds");
	expect(planned.demand_lines).toBeGreaterThan(0);
	expect(planned.shortages.length).toBe(planned.shortage_count);
	expect(planned.shortage_count, "the shortage list is the planner's exception report").toBeGreaterThan(0);
	expect(planned.proposals.length).toBe(planned.proposal_count);
	const kinds = new Set(planned.proposals.map((p: any) => p.proposal_type));
	expect([...kinds].sort()).toEqual(["Manufacture", "Purchase"]);

	stage("3. a replay from the stored inputs gives the identical plan");
	await openForm(planner.page, "Planning Run", run);
	await clickAction(planner.page, "Replay & Verify");
	await planner.page.waitForURL((url) => url.pathname.includes("/planning-run/") && !url.pathname.endsWith(run));
	const replay = runOf(planner.page.url());
	expect((await settled(replay)).replay_result).toBe("Identical");

	stage("4. the planner approves the proposals");
	await openForm(planner.page, "Planning Run", run);
	await clickAction(planner.page, "Approve All", "Proposals");
	await expect.poll(async () => (await getDoc(planner.page, "Planning Run", run)).proposals
		.every((p: any) => p.decision === "Approved")).toBe(true);

	stage("5. release: a production plan, work orders and purchase requests to act on");
	await openForm(planner.page, "Planning Run", run);
	await clickAction(planner.page, "Release", "Proposals");
	await expect.poll(() => status(run), { timeout: 120_000 }).toBe("Released");
	const released = await getDoc(planner.page, "Planning Run", run);
	expect(released.production_plan).toBeTruthy();
	const workOrders = await getList(auditor.page, "Work Order", { production_plan: released.production_plan },
		["name", "automotive_plant", "automotive_planning_run", "docstatus"], 500);
	expect(workOrders.length).toBeGreaterThan(0);
	// planned orders for the plant, released to the line by the daily sequence
	expect(workOrders.every((w) => w.automotive_plant === "AICHI-VA" && w.automotive_planning_run === run
		&& w.docstatus === 0)).toBe(true);
	const linked = released.proposals.filter((p: any) => p.document_name);
	expect(linked.some((p: any) => p.document_type === "Work Order")).toBe(true);
	expect(linked.some((p: any) => p.document_type === "Material Request")).toBe(true);
	const request = linked.find((p: any) => p.document_type === "Material Request");
	expect((await getDoc(auditor.page, "Material Request", request.document_name)).docstatus).toBe(1);
	const events = (await getList(auditor.page, "Audit Event", { reference_name: run }, ["event_type"], 20))
		.map((e) => e.event_type);
	expect(events).toContain("planning.released");
	for (const ctx of [planner, auditor]) await ctx.context.close();
});
