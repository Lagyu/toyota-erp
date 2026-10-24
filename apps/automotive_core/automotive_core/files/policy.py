"""Attachment controls: type/size limits, content sniffing, private-by-default,
malware scanning, download restriction and retention (ERP-11).
"""

from __future__ import annotations

import filetype
import frappe
from frappe import _
from frappe.utils import add_days, cint, now_datetime, nowdate

from automotive_core.governance import audit
from automotive_core.utils import commit, sha256_hex

# Attachments the framework creates for itself; size/extension rules do not apply.
SYSTEM_ATTACHMENT_DOCTYPES = {
	"Prepared Report",
	"Data Import",
	"Data Export",
	"Report",
	"Print Format",
	"Letter Head",
	"Website Settings",
	"Navbar Settings",
	"Web Page",
	"Email Queue",
	"Communication",
	"User",
}
# Extensions whose magic bytes we can and must verify.
SNIFFED = {
	"pdf": {"application/pdf"},
	"png": {"image/png"},
	"jpg": {"image/jpeg"},
	"jpeg": {"image/jpeg"},
	"xlsx": {"application/zip", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
}


class AttachmentRejectedError(frappe.ValidationError):
	pass


def _settings():
	return frappe.get_cached_doc("Automotive Settings")


def rule_for(doctype: str | None):
	for rule in _settings().attachment_rules or []:
		if rule.reference_doctype == doctype:
			return rule
	return None


def _applies(doc) -> bool:
	if doc.is_folder or frappe.flags.in_install or frappe.flags.in_migrate:
		return False
	return doc.attached_to_doctype not in SYSTEM_ATTACHMENT_DOCTYPES


def _extension(file_name: str | None) -> str:
	return (file_name or "").rsplit(".", 1)[-1].lower() if "." in (file_name or "") else ""


def validate_file(doc, method=None) -> None:
	if not doc.is_new() or not _applies(doc):
		return
	settings = _settings()
	rule = rule_for(doc.attached_to_doctype)
	allowed = (rule.allowed_extensions if rule and rule.allowed_extensions else settings.allowed_extensions) or ""
	allowed_set = {e.strip().lower().lstrip(".") for e in allowed.split(",") if e.strip()}
	ext = _extension(doc.file_name or doc.file_url)
	if allowed_set and ext not in allowed_set:
		raise AttachmentRejectedError(_("Files of type .{0} are not allowed here.").format(ext or "?"))
	max_mb = cint(rule.max_size_mb if rule and rule.max_size_mb else settings.max_upload_mb)
	if max_mb and cint(doc.file_size) > max_mb * 1024 * 1024:
		raise AttachmentRejectedError(_("File exceeds the {0} MB limit.").format(max_mb))
	if ext in SNIFFED and not doc.is_remote_file:
		content = doc.get_content()
		if isinstance(content, str):
			content = content.encode()
		kind = filetype.guess(content[:8192]) if content else None
		if not kind or kind.mime not in SNIFFED[ext]:
			raise AttachmentRejectedError(_("The file content does not match its .{0} extension.").format(ext))
	if doc.attached_to_doctype and (not rule or rule.private_only) and not doc.is_private:
		frappe.throw(_("Attachments to business documents must be private."), AttachmentRejectedError)


def after_insert(doc, method=None) -> None:
	if not _applies(doc) or doc.is_remote_file:
		return
	content = doc.get_content()
	if isinstance(content, str):
		content = content.encode()
	frappe.db.set_value("File", doc.name, "automotive_sha256", sha256_hex(content or b""), update_modified=False)
	mode = _settings().virus_scan_mode
	if mode == "Disabled":
		frappe.db.set_value("File", doc.name, "automotive_scan_status", "Not Scanned", update_modified=False)
		return
	if not _settings().clamd_host:
		frappe.db.set_value("File", doc.name, "automotive_scan_status", "Scanner Not Configured", update_modified=False)
		return
	frappe.enqueue("automotive_core.files.policy.scan_file", queue="short", file_name=doc.name,
		enqueue_after_commit=True)


def scan_file(file_name: str) -> str:
	from automotive_core.files.clamav import ClamdError, scan_bytes

	doc = frappe.get_doc("File", file_name)
	settings = _settings()
	try:
		content = doc.get_content()
		if isinstance(content, str):
			content = content.encode()
		clean, signature = scan_bytes(content or b"", settings.clamd_host, cint(settings.clamd_port) or 3310)
		status = "Clean" if clean else "Infected"
	except ClamdError as exc:
		status, signature = "Scan Failed", str(exc)
	frappe.db.set_value("File", doc.name, {"automotive_scan_status": status, "automotive_scanned_at": now_datetime()},
		update_modified=False)
	if status != "Clean":
		audit.record("file.scan_" + status.lower().replace(" ", "_"), "File", doc.name,
			{"signature": signature, "attached_to": [doc.attached_to_doctype, doc.attached_to_name]})
	commit()
	return status


def has_permission(doc, ptype=None, user=None, debug=False):
	"""Block reading unscanned/infected files when scanning is mandatory."""
	if ptype not in (None, "read", "print", "download"):
		return None
	if _settings().virus_scan_mode != "Required" or not _applies(doc):
		return None
	user = user or frappe.session.user
	if user == "Administrator":
		return None
	if (doc.get("automotive_scan_status") or "Not Scanned") != "Clean":
		return False
	return None


def apply_retention() -> int:
	"""Daily: delete attachments older than their DocType's retention period."""
	deleted = 0
	for rule in _settings().attachment_rules or []:
		if not cint(rule.retention_days):
			continue
		cutoff = add_days(nowdate(), -cint(rule.retention_days))
		names = frappe.get_all(
			"File",
			filters={"attached_to_doctype": rule.reference_doctype, "creation": ("<", cutoff), "is_folder": 0},
			pluck="name",
			limit=1000,
		)
		for name in names:
			frappe.delete_doc("File", name, ignore_permissions=True, force=True)
			deleted += 1
		if names:
			audit.record("file.retention_purge", "DocType", rule.reference_doctype,
				{"deleted": len(names), "cutoff": str(cutoff)})
	commit()
	return deleted
