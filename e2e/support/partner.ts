// A partner's system: builds Hinode EDI (or JSON) envelopes from the published format
// description (automotive_core/integration_hub/adapters/edi.py), signs them with the
// partner's HMAC secret and posts them to the inbound endpoint over HTTPS.
import { type APIRequestContext } from "@playwright/test";
import { createHash, createHmac, randomBytes } from "node:crypto";

export type Envelope = { message_id: string; message_type: string; schema_version?: string; sent_at?: string;
	data: Record<string, unknown> };

const RELEASE = "?";
const SEG = "~";
const ELEM = "*";

const escape = (value: string) =>
	value.replaceAll(RELEASE, RELEASE + RELEASE).replaceAll(ELEM, RELEASE + ELEM).replaceAll(SEG, RELEASE + SEG);

function flatten(prefix: string, value: unknown, out: string[][]) {
	if (value === null || value === undefined) {
		out.push([prefix, "Z", ""]);
	} else if (Array.isArray(value)) {
		if (!value.length) out.push([prefix, "L", ""]);
		value.forEach((item, i) => flatten(`${prefix}.${i}`, item, out));
	} else if (typeof value === "object") {
		const keys = Object.keys(value).sort();
		if (!keys.length) out.push([prefix, "O", ""]);
		for (const key of keys) flatten(prefix ? `${prefix}.${key}` : key, (value as Record<string, unknown>)[key], out);
	} else if (typeof value === "boolean") {
		out.push([prefix, "B", value ? "1" : "0"]);
	} else if (typeof value === "number") {
		out.push([prefix, "N", String(value)]);
	} else {
		out.push([prefix, "S", String(value)]);
	}
}

export function encodeEdi(envelope: Envelope): string {
	const control = createHash("sha256").update(envelope.message_id).digest("hex").slice(0, 9);
	const leaves: string[][] = [];
	flatten("", envelope.data ?? {}, leaves);
	const segments = [
		["ISA", "HINODE", "1", control],
		["ST", envelope.message_type, envelope.message_id],
		["BGN", envelope.schema_version ?? "v1", envelope.sent_at ?? ""],
		...leaves.map(([path, kind, value]) => ["DTA", path, kind, value]),
	];
	segments.push(["SE", String(segments.length), envelope.message_id]);
	segments.push(["IEA", "1", control]);
	return segments.map((segment) => segment.map(escape).join(ELEM) + SEG + "\n").join("");
}

export function signature(secret: string, body: string): string {
	const t = Math.floor(Date.now() / 1000);
	const n = randomBytes(16).toString("hex");
	const v1 = createHmac("sha256", secret).update(`${t}.${n}.`).update(body).digest("hex");
	return `t=${t},kid=e2e,n=${n},v1=${v1}`;
}

/** POST one message; every call is a new signed request (fresh nonce), as a partner's retry would be. */
export async function deliver(request: APIRequestContext, partner: { partner: string; secret: string; format: string },
		envelope: Envelope): Promise<{ status: number; body: any }> {
	const edi = partner.format === "Hinode EDI";
	const body = edi ? encodeEdi(envelope) : JSON.stringify(envelope);
	const response = await request.post("/api/method/automotive_core.integration_hub.inbound.receive", {
		headers: {
			"Content-Type": edi ? "text/plain" : "application/json",
			"X-Hinode-Partner": partner.partner,
			"X-Hinode-Signature": signature(partner.secret, body),
		},
		data: body,
	});
	return { status: response.status(), body: (await response.json().catch(() => ({}))).message };
}
