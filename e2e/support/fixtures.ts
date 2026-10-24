// Journey preconditions written by `bench execute automotive_seed.e2e.prepare_journeys`
// (staged material for a build to order, a variant in stock, an EDI partner's secret).
import { readFileSync } from "node:fs";

export type Variant = { variant: string; model: string; selections: Record<string, string>; line: string };

export type Fixtures = {
	dealer: string;
	company: string;
	j01: (Variant & { bom: string; line_side: string; plant: string; wip_warehouse: string; fg_warehouse: string;
		staged: { item_code: string; qty: number }[] }) | null;
	j04: { item: string; bom: string; component: string; qty: number; new_qty: number; work_order: string } | null;
	j05: { item_code: string; batch_no: string; supplier: string; line: string; workstation: string; line_side: string;
		store: string; quarantine: string; line_side_qty: number; purchase_receipt: string; receipt_row: string;
		company: string } | null;
	j06: { purchase_receipt: string; supplier: string; company: string; paid_from: string; bank_account: string } | null;
	j07: { item_code: string; rate: number; seller: string; buyer: string; supplier: string; seller_warehouse: string;
		buyer_warehouse: string } | null;
	j08: { vin: string; dealer: string; batch_item: string; batch_no: string; batch_supplier: string; batch_level: number;
		serial_item: string; serial_no: string } | null;
	j09: Variant | null;
	j10: { partner: string; supplier: string; format: string; secret: string; purchase_order: string; po_item: string;
		item_code: string; qty: number; has_batch_no: number } | null;
};

export function fixtures(): Fixtures {
	const path = process.env.E2E_FIXTURES;
	if (!path) throw new Error("E2E_FIXTURES is not set (see scripts/ci/e2e.sh)");
	return JSON.parse(readFileSync(path, "utf-8"));
}
