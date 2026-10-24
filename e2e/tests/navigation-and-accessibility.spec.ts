// GATE-1, ERP-12.2, ERP-12.3: no dead navigation, and accessible partner portals.
// Dead links: for each function's persona, every sidebar and workspace link the desk offers
// opens for that person (the DocType list, the report or the page exists and they may read
// it). Accessibility: the supplier and dealer portals, on every tab, have no serious or
// critical WCAG 2.1 A/AA violations (axe-core); the desk login page has no critical ones.
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";
import { call, openDesk } from "../support/frappe";
import { as, type Persona } from "../support/personas";

const DESK_PERSONAS: Persona[] = ["buyer", "ap_clerk", "treasury", "controller", "executive", "planner", "supervisor",
	"operator", "warehouse", "inspector", "quality_manager", "engineer", "logistics", "dealer_liaison", "auditor",
	"it_operator"];

type Target = { type: string; to: string; from: string };

/** Every link_type/link_to pair anywhere in an object (sidebars, workspace cards, shortcuts). */
function collect(node: unknown, from: string, out: Map<string, Target>) {
	if (Array.isArray(node)) {
		for (const item of node) collect(item, from, out);
	} else if (node && typeof node === "object") {
		const o = node as Record<string, unknown>;
		const type = (o.link_type ?? o.type) as string | undefined;
		const to = o.link_to as string | undefined;
		if (to && type && ["DocType", "Report", "Page"].includes(type)) out.set(`${type}:${to}`, { type, to, from });
		for (const value of Object.values(o)) if (value && typeof value === "object") collect(value, from, out);
	}
}

async function opens(page: Page, target: Target): Promise<string | null> {
	try {
		if (target.type === "DocType") await call(page, "frappe.desk.reportview.get_count", { doctype: target.to });
		else if (target.type === "Report") await call(page, "frappe.desk.query_report.get_script", { report_name: target.to });
		else await call(page, "frappe.desk.desk_page.get", { name: target.to });
		return null;
	} catch (e) {
		return `${target.type} ${target.to} (from ${target.from}): ${String((e as Error).message).slice(0, 160)}`;
	}
}

for (const persona of DESK_PERSONAS) {
	test(`every desk link ${persona} is offered opens for them`, async ({ browser }) => {
		const { context, page } = await as(browser, persona);
		await openDesk(page);
		const boot = await page.evaluate(() => {
			const b = (window as any).frappe.boot;
			return { sidebars: b.module_sidebars || {}, workspaces: (b.allowed_workspaces || []).map((w: any) => w.name) };
		});
		const targets = new Map<string, Target>();
		collect(boot.sidebars, "sidebar", targets);
		for (const name of boot.workspaces as string[]) {
			const desk = await call(page, "frappe.desk.desktop.get_desktop_page",
				{ page: JSON.stringify({ name, title: name, public: 1 }) });
			collect(desk, `workspace ${name}`, targets);
		}
		expect(targets.size, `${persona} has navigation`).toBeGreaterThan(0);
		const dead = (await Promise.all([...targets.values()].map((t) => opens(page, t)))).filter(Boolean);
		expect(dead, `dead links for ${persona}`).toEqual([]);
		await context.close();
	});
}

const SERIOUS = ["serious", "critical"];
const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

async function violations(page: Page, impacts: string[]) {
	const result = await new AxeBuilder({ page }).withTags(WCAG).analyze();
	return result.violations.filter((v) => impacts.includes(v.impact ?? ""))
		.map((v) => `${v.id} (${v.impact}): ${v.help} — ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(", ")}`);
}

for (const [persona, path] of [["supplier", "/supplier-portal"], ["dealer", "/dealer-portal"]] as const) {
	test(`the ${persona} portal has no serious accessibility violations on any tab`, async ({ browser }) => {
		const { context, page } = await as(browser, persona);
		await page.goto(path);
		const tabs = await page.getByRole("tab").allTextContents();
		expect(tabs.length).toBeGreaterThan(1);
		const found: string[] = [];
		for (const tab of tabs) {
			await page.getByRole("tab", { name: tab, exact: true }).click();
			await expect(page.locator(".hp-loading")).toHaveCount(0);
			found.push(...(await violations(page, SERIOUS)).map((v) => `[${tab}] ${v}`));
		}
		expect(found).toEqual([]);
		await context.close();
	});
}

test("the sign-in page has no critical accessibility violations", async ({ browser }) => {
	const context = await browser.newContext();
	const page = await context.newPage();
	await page.goto("/login");
	expect(await violations(page, ["critical"])).toEqual([]);
	await context.close();
});
