// Persona sessions. E2E_CREDENTIALS points at the JSON written by
// `bench execute automotive_seed.e2e.prepare_users` ({"password", "users": {persona: email}}).
import { type Browser, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

export type Persona =
	| "administrator" | "executive" | "controller" | "buyer" | "procurement_manager" | "ap_clerk" | "treasury"
	| "treasury_2" | "sales_manager" | "dealer_liaison" | "it_operator" | "auditor" | "plant_director" | "planner"
	| "supervisor" | "operator" | "warehouse" | "inspector" | "quality_manager" | "engineer" | "engineering_manager"
	| "logistics" | "na_warehouse" | "kyushu_logistics" | "supplier" | "dealer";

export function credentials(): { password: string; users: Record<string, string> } {
	const path = process.env.E2E_CREDENTIALS;
	if (!path) throw new Error("E2E_CREDENTIALS is not set (see scripts/ci/e2e.sh)");
	return JSON.parse(readFileSync(path, "utf-8"));
}

export const statePath = (persona: string) => `.auth/${persona}.json`;

export function userOf(persona: Persona): string {
	const users = JSON.parse(readFileSync(".auth/users.json", "utf-8"));
	return users[persona];
}

export async function as(browser: Browser, persona: Persona): Promise<{ context: BrowserContext; page: Page }> {
	const context = await browser.newContext({ storageState: statePath(persona) });
	const page = await context.newPage();
	return { context, page };
}
