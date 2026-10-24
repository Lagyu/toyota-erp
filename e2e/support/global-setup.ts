// Logs every persona in once (POST /api/method/login) and stores its session, so tests
// start as real dataset users with their real roles and scopes.
import { request, type FullConfig } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { credentials, statePath } from "./personas";

export default async function globalSetup(config: FullConfig) {
	const baseURL = config.projects[0].use.baseURL as string;
	const creds = credentials();
	mkdirSync(".auth", { recursive: true });
	for (const [persona, user] of Object.entries(creds.users)) {
		if (!user) continue;
		const ctx = await request.newContext({ baseURL });
		const res = await ctx.post("/api/method/login", { data: { usr: user, pwd: creds.password } });
		if (!res.ok()) {
			// the server's reason (wrong password, disabled user, locked account), never the password
			const reason = (await res.text()).slice(0, 200);
			throw new Error(`login failed for ${persona} (${user}): ${res.status()} ${reason}`);
		}
		await ctx.storageState({ path: statePath(persona) });
		await ctx.dispose();
	}
	writeFileSync(".auth/users.json", JSON.stringify(creds.users, null, 1));
}
