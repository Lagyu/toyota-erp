"""Local file-drop exchange: persistent, durable end-to-end processing while a partner
has no live connection (requirements ERP-11: "local adapters with persistent
end-to-end processing"). Files are written atomically (temp file + rename).

    <site>/private/integration/<partner>/outbox/   messages we send
    <site>/private/integration/<partner>/inbox/    messages the partner drops for us
    <site>/private/integration/<partner>/processed/  accepted inbox files
    <site>/private/integration/<partner>/rejected/   inbox files that failed validation
"""

from __future__ import annotations

import os
from pathlib import Path

import frappe

from automotive_core.integration_hub.adapters.base import Adapter, DeliveryResult
from automotive_core.utils import commit

SUBDIRS = ("outbox", "inbox", "processed", "rejected")


def partner_dir(partner_code: str) -> Path:
	base = Path(frappe.get_site_path("private", "integration", partner_code)).resolve()
	for sub in SUBDIRS:
		(base / sub).mkdir(parents=True, exist_ok=True)
	return base


def atomic_write(path: Path, data: bytes) -> None:
	tmp = path.with_suffix(path.suffix + ".tmp")
	with open(tmp, "wb") as handle:
		handle.write(data)
		handle.flush()
		os.fsync(handle.fileno())
	os.replace(tmp, path)


class FileDropAdapter(Adapter):
	name = "Local File Drop"

	def send(self, partner, message) -> DeliveryResult:
		body, content_type = self.body(partner, message)
		suffix = ".edi" if content_type == "application/edi-x12" else ".json"
		target = partner_dir(partner.name) / "outbox" / f"{message.name}{suffix}"
		try:
			atomic_write(target, body)
		except OSError as exc:
			return DeliveryResult(ok=False, status_code=0, error=f"write failed: {exc}")
		return DeliveryResult(ok=True, status_code=200, response_excerpt=str(target.name))


def poll_inbox(partner_code: str) -> dict[str, int]:
	"""Accept every file in the partner's inbox; move it to processed/ or rejected/."""
	from automotive_core.integration_hub.inbound import accept

	base = partner_dir(partner_code)
	counts = {"accepted": 0, "duplicate": 0, "rejected": 0}
	for path in sorted((base / "inbox").iterdir()):
		if path.suffix not in (".json", ".edi") or not path.is_file():
			continue
		try:
			frappe.db.savepoint("file_drop")
			result = accept(partner_code, path.read_bytes(), source="File Drop")
			counts[result["status"]] += 1
			os.replace(path, base / "processed" / path.name)
		except Exception as exc:
			frappe.db.rollback(save_point="file_drop")
			counts["rejected"] += 1
			atomic_write(base / "rejected" / (path.name + ".error.txt"), str(exc).encode())
			os.replace(path, base / "rejected" / path.name)
		commit()
	return counts
