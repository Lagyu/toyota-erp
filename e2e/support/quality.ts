// Inspection results as an inspector records them. An item with an inspection plan gets
// one reading per parameter, and ERPNext decides Accepted or Rejected from the readings. In
// a failing inspection, the first parameter is out of tolerance. An item without a plan is
// inspected manually, with the status given.
import type { Page } from "@playwright/test";
import { getDoc } from "./frappe";

type Parameter = { specification: string; numeric: number; min_value: number; max_value: number; value: string };

export async function inspectionResult(page: Page, item: string, passes: boolean): Promise<Record<string, unknown>> {
	const template = (await getDoc(page, "Item", item)).quality_inspection_template;
	if (!template) return { manual_inspection: 1, status: passes ? "Accepted" : "Rejected" };
	const plan = await getDoc(page, "Quality Inspection Template", template);
	const readings = (plan.item_quality_inspection_parameter as Parameter[]).map((p, i) => {
		const failing = !passes && i === 0;
		if (!p.numeric) {
			return { specification: p.specification, numeric: 0, value: p.value, reading_value: failing ? "NG" : p.value };
		}
		const span = p.max_value - p.min_value;
		const reading = failing ? p.max_value + span * 0.2 : p.min_value + span / 2;
		return { specification: p.specification, numeric: 1, min_value: p.min_value, max_value: p.max_value,
			reading_1: reading.toFixed(3) };
	});
	return { quality_inspection_template: template, manual_inspection: 0, readings };
}
