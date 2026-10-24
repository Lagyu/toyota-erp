// Journey preconditions written by `bench execute automotive_seed.e2e.prepare_journeys`
// (staged material for a build to order, a variant in stock, an EDI partner's secret).
import { readFileSync } from "node:fs";

export type Variant = { variant: string; model: string; selections: Record<string, string>; line: string };

export type Fixtures = {
	dealer: string;
	company: string;
	j01: (Variant & { bom: string; line_side: string; plant: string; wip_warehouse: string; fg_warehouse: string;
		staged: { item_code: string; qty: number }[] }) | null;
	j09: Variant | null;
	j10: { partner: string; supplier: string; format: string; secret: string; purchase_order: string; po_item: string;
		item_code: string; qty: number; has_batch_no: number } | null;
};

export function fixtures(): Fixtures {
	const path = process.env.E2E_FIXTURES;
	if (!path) throw new Error("E2E_FIXTURES is not set (see scripts/ci/e2e.sh)");
	return JSON.parse(readFileSync(path, "utf-8"));
}
