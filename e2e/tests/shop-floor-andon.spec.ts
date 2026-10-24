// Shop floor on the station tablet (ERP-04): an operator calls for help from the andon form;
// the supervisor responds and closes the call. Calls do not stop the line or hold work.
import { expect, test } from "@playwright/test";
import { clickAction, getDoc, openDesk, openForm } from "../support/frappe";
import { as } from "../support/personas";

test("an operator calls for help from the station; the supervisor responds and closes it", async ({ browser }) => {
	const operator = await as(browser, "operator");
	await openDesk(operator.page, "/desk/andon-event/new");
	const [station] = (await getDoc(operator.page, "Production Line", "AICHI-FA1")).stations;
	const form = operator.page.locator(".form-layout");
	for (const [fieldname, value] of [["production_line", "AICHI-FA1"], ["workstation", station.workstation],
			["reason_code", "P-HELP"]] as const) {
		const input = form.locator(`.frappe-control[data-fieldname="${fieldname}"] input`).first();
		await input.fill(value);
		await operator.page.locator(".awesomplete [role=option]", { hasText: value }).first().click();
	}
	await form.locator('.frappe-control[data-fieldname="stop_type"] select').selectOption("Call");
	await form.locator('.frappe-control[data-fieldname="description"] textarea').fill("Torque gun calibration doubt.");
	await operator.page.keyboard.press("Control+s");
	await operator.page.waitForURL(/andon-event\/AND-/);
	const andon = decodeURIComponent(operator.page.url().split("/andon-event/")[1].split(/[?#]/)[0]);
	const raised = await getDoc(operator.page, "Andon Event", andon);
	expect(raised).toMatchObject({ status: "Raised", category: "Process", stop_type: "Call" });
	expect(raised.quality_hold ?? null).toBeNull(); // a call holds nothing

	const supervisor = await as(browser, "supervisor");
	await openForm(supervisor.page, "Andon Event", andon);
	await clickAction(supervisor.page, "Acknowledge");
	await expect.poll(async () => (await getDoc(supervisor.page, "Andon Event", andon)).status).toBe("Acknowledged");
	await openForm(supervisor.page, "Andon Event", andon);
	await clickAction(supervisor.page, "Close");
	await expect.poll(async () => (await getDoc(supervisor.page, "Andon Event", andon)).status).toBe("Closed");
	for (const ctx of [operator, supervisor]) await ctx.context.close();
});
