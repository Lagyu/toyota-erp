// J-08: finished-vehicle genealogy -> component search -> affected vehicle list. Quality
// works from the reports on the desk; the traceability must be complete upstream (to the
// supplier lot, through sub-assemblies) and downstream (to the dealer), and reproducible.
import { expect, test, type Page } from "@playwright/test";
import { clickAction, getDoc, insertDoc, openDesk, openForm } from "../support/frappe";
import { fixtures } from "../support/fixtures";
import { stages } from "../support/journey";
import { as } from "../support/personas";

async function runReport(page: Page, report: string, filters: Record<string, string>): Promise<any[]> {
	const query = new URLSearchParams(filters).toString();
	await openDesk(page, `/desk/query-report/${encodeURIComponent(report)}?${query}`);
	await page.waitForFunction(() => ((window as any).frappe?.query_report?.data?.length ?? 0) > 0, null,
		{ timeout: 60_000 });
	await expect(page.locator(".dt-scrollable, .datatable").first()).toBeVisible();
	return page.evaluate(() => (window as any).frappe.query_report.data);
}

test("J-08 a vehicle's genealogy leads to its supplier lots and back to every affected vehicle", async ({ browser }) => {
	const fx = fixtures().j08;
	test.skip(!fx, "no delivered vehicle with a multi-level genealogy on this dataset");
	if (!fx) return;
	const stage = stages("J-08");
	const quality = await as(browser, "quality_manager");

	stage("1. quality opens the vehicle's genealogy: components, sub-assemblies and their lots");
	const tree = await runReport(quality.page, "Vehicle Genealogy", { serial_no: fx.vin });
	expect(tree.find((r) => r.component_serial_no === fx.serial_no), "the serialised component").toBeTruthy();
	const deep = tree.find((r) => r.component_batch_no === fx.batch_no);
	expect(deep, "the supplier lot inside a sub-assembly").toMatchObject({ level: fx.batch_level,
		supplier: fx.batch_supplier });
	expect(tree.some((r) => r.component_batch_no === deep.parent || r.component_serial_no === deep.parent),
		"the sub-assembly that carries the lot is itself in the vehicle").toBe(true);

	stage("2. component search: every vehicle built with that supplier lot");
	const affected = await runReport(quality.page, "Affected Vehicles", { item_code: fx.batch_item, batch_nos: fx.batch_no });
	const ours = affected.find((r) => r.serial_no === fx.vin);
	expect(ours, "the vehicle is in the affected list").toMatchObject({ dealer: fx.dealer });
	expect(ours.path).toContain(fx.batch_no);

	stage("3. a recall campaign reproduces the list and records where each vehicle is");
	await openDesk(quality.page);
	const recall = await insertDoc(quality.page, { doctype: "Recall Campaign",
		title: `Sealer lot ${fx.batch_no}`, component_item: fx.batch_item, batch_nos: fx.batch_no });
	await openForm(quality.page, "Recall Campaign", recall.name);
	await clickAction(quality.page, "Analyze Affected Units");
	await expect.poll(async () => (await getDoc(quality.page, "Recall Campaign", recall.name)).status).toBe("Analyzed");
	const first = await getDoc(quality.page, "Recall Campaign", recall.name);
	expect(first.affected_units).toBe(affected.length);
	expect(first.affected.find((r: any) => r.unit_serial_no === fx.vin)).toMatchObject({ dealer: fx.dealer });
	expect(first.affected_at_dealers).toBeGreaterThanOrEqual(1);

	stage("4. the analysis is reproducible: same query, same result fingerprint");
	await openForm(quality.page, "Recall Campaign", recall.name);
	await clickAction(quality.page, "Analyze Affected Units");
	await expect.poll(async () => (await getDoc(quality.page, "Recall Campaign", recall.name)).analyzed_at)
		.not.toBe(first.analyzed_at);
	const again = await getDoc(quality.page, "Recall Campaign", recall.name);
	expect([again.query_sha256, again.result_sha256]).toEqual([first.query_sha256, first.result_sha256]);
	await quality.context.close();
});
