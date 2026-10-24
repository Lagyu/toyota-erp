"""Attachment controls: type/size limits, content sniffing, private-by-default,
malware scanning, download restriction and retention (ERP-11).
"""

from __future__ import annotations

import filetype
import frappe
from frappe import _
from frappe.core.doctype.file.file import File
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
	# ERPNext internals: reposting keeps its affected-transaction log as a gzip attachment
	# (stock_ledger.update_args_in_repost_item_valuation); statement imports keep their CSV
	"Repost Item Valuation",
	"Bank Statement Import",
	"Bank Statement Import Log",
	"Code List",
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


class AutomotiveFile(File):
	"""Frappe's File, refusing unreadable content cleanly: Frappe parses an uploaded PDF
	(looking for JavaScript) before any validation, and a file that only claims to be a PDF
	made the parser fail with a server error instead of a refusal."""

	def before_insert(self):
		try:
			super().before_insert()
		except Exception as exc:
			if type(exc).__module__.split(".")[0] != "pypdf":
				raise
			raise AttachmentRejectedError(
				_("The file content does not match its .{0} extension.").format(
					_extension(self.file_name or self.file_url) or "?"
				)
			) from exc


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
	allowed = (
		rule.allowed_extensions if rule and rule.allowed_extensions else settings.allowed_extensions
	) or ""
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
			raise AttachmentRejectedError(
				_("The file content does not match its .{0} extension.").format(ext)
			)
	if doc.attached_to_doctype and (not rule or rule.private_only) and not doc.is_private:
		frappe.throw(_("Attachments to business documents must be private."), AttachmentRejectedError)


def after_insert(doc, method=None) -> None:
	if not _applies(doc) or doc.is_remote_file:
		return
	content = doc.get_content()
	if isinstance(content, str):
		content = content.encode()
	frappe.db.set_value(
		"File", doc.name, "automotive_sha256", sha256_hex(content or b""), update_modified=False
	)
	mode = _settings().virus_scan_mode
	if mode == "Disabled":
		frappe.db.set_value("File", doc.name, "automotive_scan_status", "Not Scanned", update_modified=False)
		return
	if not _settings().clamd_host:
		frappe.db.set_value(
			"File", doc.name, "automotive_scan_status", "Scanner Not Configured", update_modified=False
		)
		return
	frappe.enqueue(
		"automotive_core.files.policy.scan_file", queue="short", file_name=doc.name, enqueue_after_commit=True
	)


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
	except OSError as exc:  # the file's content is gone from the disk
		status, signature = "Scan Failed", f"content unreadable: {exc}"
	frappe.db.set_value(
		"File",
		doc.name,
		{"automotive_scan_status": status, "automotive_scanned_at": now_datetime()},
		update_modified=False,
	)
	if status != "Clean":
		audit.record(
			"file.scan_" + status.lower().replace(" ", "_"),
			"File",
			doc.name,
			{"signature": signature, "attached_to": [doc.attached_to_doctype, doc.attached_to_name]},
		)
	commit()
	return status


RESCAN_BATCH = 200
PENDING = ("", "Not Scanned", "Scanner Not Configured", "Scan Failed")


def rescan_pending(limit: int = RESCAN_BATCH) -> int:
	"""Hourly: scan the files that have no result yet, least recently tried first and at most
	``limit`` a run: those uploaded before scanning was enabled (Not Scanned, Scanner Not
	Configured) and those missed at upload because clamd was down (Scan Failed). Nothing happens
	while scanning is Disabled, no clamd host is set or clamd does not answer: a run with clamd
	down would only record the same failures again. Returns the number of files scanned."""
	from automotive_core.files.clamav import ping

	settings = _settings()
	if settings.virus_scan_mode == "Disabled" or not settings.clamd_host:
		return 0
	if not ping(settings.clamd_host, cint(settings.clamd_port) or 3310):
		return 0
	# a remote file (a URL) has no content here to scan
	names = frappe.db.sql(
		"""select name from `tabFile` where is_folder = 0 and ifnull(file_url, '') not like 'http%%'
		and ifnull(automotive_scan_status, '') in %(pending)s
		and ifnull(attached_to_doctype, '') not in %(system)s
		order by ifnull(automotive_scanned_at, '2000-01-01'), creation limit %(limit)s""",
		{"pending": PENDING, "system": tuple(SYSTEM_ATTACHMENT_DOCTYPES) or ("",), "limit": cint(limit)},
		pluck=True,
	)
	for name in names:
		scan_file(name)
	return len(names)


def has_permission(doc, ptype=None, user=None, debug=False):
	"""Block reading unscanned/infected files when scanning is mandatory. A controller check can
	only take a right away: Frappe reads any falsy answer (None included) as a refusal, so "no
	objection" is True."""
	if ptype not in (None, "read", "print", "download"):
		return True
	if _settings().virus_scan_mode != "Required" or not _applies(doc):
		return True
	user = user or frappe.session.user
	if user == "Administrator":
		return True
	return (doc.get("automotive_scan_status") or "Not Scanned") == "Clean"


# the default period never purges the system's own attachments, nor master data's
KEPT_DOCTYPES = SYSTEM_ATTACHMENT_DOCTYPES | {
	"Company",
	"Print Heading",
	"Web Form",
	"Email Template",
	"Workspace",
	"Address Template",
}


def _purge(filters: list, days: int, label: str) -> int:
	cutoff = add_days(nowdate(), -cint(days))
	names = frappe.get_all(
		"File", filters=[*filters, ["creation", "<", cutoff], ["is_folder", "=", 0]], pluck="name", limit=1000
	)
	for name in names:
		frappe.delete_doc("File", name, ignore_permissions=True, force=True)
	if names:
		audit.record(
			"file.retention_purge",
			"DocType",
			label if frappe.db.exists("DocType", label) else None,
			{"deleted": len(names), "cutoff": str(cutoff), "rule": label},
		)
	return len(names)


def apply_retention() -> int:
	"""Daily: delete attachments older than their DocType's retention period; DocTypes without
	a rule follow the default period (Automotive Settings; 0 keeps them), except the
	system's own records (KEPT_DOCTYPES)."""
	settings = _settings()
	deleted, ruled = 0, set()
	for rule in settings.attachment_rules or []:
		ruled.add(rule.reference_doctype)
		if cint(rule.retention_days):
			deleted += _purge(
				[["attached_to_doctype", "=", rule.reference_doctype]],
				rule.retention_days,
				rule.reference_doctype,
			)
	if cint(settings.default_retention_days):
		deleted += _purge(
			[
				["attached_to_doctype", "is", "set"],
				["attached_to_doctype", "not in", sorted(ruled | set(KEPT_DOCTYPES))],
			],
			settings.default_retention_days,
			"default",
		)
	commit()
	return deleted
