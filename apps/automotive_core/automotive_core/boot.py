"""Desk boot info: which DocTypes get the approval panel and cancel-with-reason."""

from __future__ import annotations

import frappe

from automotive_core.governance.approvals import governed_doctypes
from automotive_core.utils import hook_list


def boot_session(bootinfo) -> None:
	if frappe.session.user == "Guest":
		return
	bootinfo.automotive = {
		"governed_doctypes": sorted(governed_doctypes()),
		"cancel_reason_doctypes": hook_list("automotive_cancel_reason_doctypes"),
	}
