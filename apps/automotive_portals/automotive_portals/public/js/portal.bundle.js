// Entry point for /supplier-portal and /dealer-portal (see www/).
import { mountDealer } from "./portal/dealer";
import { mountSupplier } from "./portal/supplier";

function boot() {
	const root = document.getElementById("hinode-portal");
	if (!root) return;
	const options = { party: root.dataset.partyName || "" };
	if (root.dataset.portal === "supplier") mountSupplier(root, options);
	else if (root.dataset.portal === "dealer") mountDealer(root, options);
}

if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
else boot();
