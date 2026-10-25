// GATE-1, ERP-12.2, ERP-12.3: no dead navigation, and accessible partner portals.
// Dead links: for each function's persona, every sidebar and workspace link the desk offers
// opens for that person (the DocType list, the report or the page exists and they may read
// it). Accessibility: the supplier and dealer portals, on every tab, have no serious or
// critical WCAG 2.1 A/AA violations (axe-core); the desk login page and the forms of the
// apps' own documents (the form body: the fields of the DocType) have no critical ones.
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";
import { call, getList, openDesk, openForm } from "../support/frappe";
import { as, type Persona } from "../support/personas";

// one persona per internal role profile (the 20 of setup/roles.py)
const DESK_PERSONAS: Persona[] = ["buyer", "procurement_manager", "ap_clerk", "treasury", "controller", "executive",
	"plant_director", "planner", "supervisor", "operator", "warehouse", "inspector", "quality_manager", "engineer",
	"engineering_manager", "logistics", "sales_manager", "dealer_liaison", "auditor", "it_operator"];

type Target = { type: string; to: string; from: string };

/** Every link_type/link_to pair anywhere in an object (sidebars, workspace cards, shortcuts).
 * The desk's boot objects hold back-references (a sidebar item points at its parent), so each
 * object is visited once. */
function collect(node: unknown, from: string, out: Map<string, Target>, seen = new Set<object>()) {
	if (!node || typeof node !== "object" || seen.has(node)) return;
	seen.add(node);
	if (Array.isArray(node)) {
		for (const item of node) collect(item, from, out, seen);
	} else {
		const o = node as Record<string, unknown>;
		const type = (o.link_type ?? o.type) as string | undefined;
		const to = o.link_to as string | undefined;
		if (to && type && ["DocType", "Report", "Page"].includes(type)) out.set(`${type}:${to}`, { type, to, from });
		for (const value of Object.values(o)) collect(value, from, out, seen);
	}
}

/** Whether the target opens for the signed-in user, by the call the desk makes to open it: a
 * list's count, a single DocType's form, a report's script, a page. */
async function opens(page: Page, target: Target, singles: Set<string>): Promise<string | null> {
	try {
		if (target.type === "DocType" && singles.has(target.to)) {
			await call(page, "frappe.desk.form.load.getdoc", { doctype: target.to, name: target.to });
		} else if (target.type === "DocType") {
			await call(page, "frappe.desk.reportview.get_count", { doctype: target.to });
		} else if (target.type === "Report") {
			await call(page, "frappe.desk.query_report.get_script", { report_name: target.to });
		} else {
			await call(page, "frappe.desk.desk_page.getpage", { name: target.to });
		}
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
			return { sidebars: b.module_sidebars || {}, workspaces: (b.allowed_workspaces || []).map((w: any) => w.name),
				singles: (b.single_types || []) as string[] };
		});
		const targets = new Map<string, Target>();
		collect(boot.sidebars, "sidebar", targets);
		for (const name of boot.workspaces as string[]) {
			const desk = await call(page, "frappe.desk.desktop.get_desktop_page",
				{ page: JSON.stringify({ name, title: name, public: 1 }) });
			collect(desk, `workspace ${name}`, targets);
		}
		expect(targets.size, `${persona} has navigation`).toBeGreaterThan(0);
		const singles = new Set(boot.singles);
		const dead = (await Promise.all([...targets.values()].map((t) => opens(page, t, singles)))).filter(Boolean);
		expect(dead, `dead links for ${persona}`).toEqual([]);
		await context.close();
	});
}

const SERIOUS = ["serious", "critical"];
const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];

async function violations(page: Page, impacts: string[]) {
	const result = await new AxeBuilder({ page }).withTags(WCAG).analyze();
	const where = (n: { target: unknown[]; any: { data?: any }[] }) => {
		const data = n.any.find((check) => check.data?.contrastRatio)?.data;
		const colours = data ? ` [${data.fgColor} on ${data.bgColor}: ${data.contrastRatio}]` : "";
		return `${n.target.join(" ")}${colours}`;
	};
	return result.violations.filter((v) => impacts.includes(v.impact ?? ""))
		.map((v) => `${v.id} (${v.impact}): ${v.help} — ${v.nodes.slice(0, 8).map(where).join(", ")}`);
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

// the forms of documents the apps define, one per domain, as the auditor reads them
const DESK_FORMS = ["Andon Event", "Kanban Signal", "Vehicle Unit", "Dealer Order", "Engineering Change Order",
	"Advance Shipping Notice", "Approval Request", "Integration Inbound Message"];

test("the apps' desk forms have no critical accessibility violations", async ({ browser }) => {
	const { context, page } = await as(browser, "auditor");
	await openDesk(page);
	const found: string[] = [];
	for (const doctype of DESK_FORMS) {
		const [doc] = await getList<any>(page, doctype, {}, ["name"], 1);
		expect(doc, `a ${doctype} in the dataset`).toBeTruthy();
		await openForm(page, doctype, doc.name);
		await expect(page.locator(".form-layout").first()).toBeVisible();
		const result = await new AxeBuilder({ page }).include(".form-layout").withTags(WCAG).analyze();
		found.push(...result.violations.filter((v) => v.impact === "critical")
			.map((v) => `[${doctype}] ${v.id}: ${v.help} — ${v.nodes.slice(0, 5).map((n) => n.target.join(" ")).join(", ")}`));
	}
	expect(found).toEqual([]);
	await context.close();
});
