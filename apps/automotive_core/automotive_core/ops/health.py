"""Health, readiness and metrics endpoints for orchestration and monitoring (§6 ops).

    GET /api/method/automotive_core.ops.health.live       process is serving (no I/O)
    GET /api/method/automotive_core.ops.health.ready      database, cache, queue, scheduler
    GET /api/method/automotive_core.ops.health.metrics    Prometheus text, bearer token

``live`` and ``ready`` are anonymous and reveal only component states. ``metrics`` requires
``Authorization: Bearer <automotive_metrics_token>`` (site config) and is never served
without a configured token. Every metric is read from the live system: queue lengths from
Redis, work backlogs from their DocTypes, the last successful backup from the marker the
backup job writes; nothing is cached or estimated.
"""

from __future__ import annotations

import hmac
import time
from pathlib import Path

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from automotive_core.utils import hook_list

# outside private/backups: bench backup prunes that directory by age (keep_backups_for_hours)
BACKUP_MARKER = ("private", "ops", "backup-last-success.txt")


def _respond(payload: dict, status: int) -> dict:
	frappe.local.response.http_status_code = status
	frappe.local.response_headers["Cache-Control"] = "no-store"
	return payload


@frappe.whitelist(allow_guest=True, methods=["GET"])
def live() -> dict:
	return {"status": "ok"}


def _check(fn) -> tuple[bool, str]:
	started = time.monotonic()
	try:
		fn()
	except Exception as exc:  # a failing dependency is a readiness result, not a crash
		return False, type(exc).__name__
	return True, f"{(time.monotonic() - started) * 1000:.0f}ms"


def _scheduler_ok() -> None:
	from frappe.utils.scheduler import is_scheduler_inactive

	if frappe.conf.get("automotive_environment") in ("production", "staging") and is_scheduler_inactive(verbose=False):
		raise RuntimeError("scheduler inactive")


@frappe.whitelist(allow_guest=True, methods=["GET"])
def ready() -> dict:
	from frappe.utils.background_jobs import get_redis_conn

	checks = {
		"database": _check(lambda: frappe.db.sql("select 1")),
		"cache": _check(lambda: frappe.cache.ping()),
		"queue": _check(lambda: get_redis_conn().ping()),
		"scheduler": _check(_scheduler_ok),
	}
	ok = all(passed for passed, _ in checks.values())
	payload = {"status": "ok" if ok else "unavailable",
		"checks": {name: {"ok": passed, "detail": detail} for name, (passed, detail) in checks.items()}}
	return _respond(payload, 200 if ok else 503)


def _authorised() -> bool:
	token = frappe.conf.get("automotive_metrics_token")
	if not token:
		return False
	header = frappe.get_request_header("Authorization") or ""
	scheme, _, presented = header.partition(" ")
	return scheme.lower() == "bearer" and hmac.compare_digest(presented.strip(), str(token))


def _count(doctype: str, filters: dict) -> int:
	return cint(frappe.db.count(doctype, filters))


def collect() -> list[tuple[str, str, dict, float]]:
	"""(name, help, labels, value) for every metric."""
	from frappe.utils.background_jobs import get_queue, get_queue_list
	from rq.registry import FailedJobRegistry

	site = frappe.local.site
	out: list[tuple[str, str, dict, float]] = []
	for queue_name in get_queue_list():
		queue = get_queue(queue_name)
		out.append(("erp_queue_jobs", "Jobs waiting in an RQ queue", {"queue": queue_name}, float(queue.count)))
		out.append(("erp_queue_failed_jobs", "Jobs in the failed registry", {"queue": queue_name},
			float(len(FailedJobRegistry(queue=queue)))))
	for status in ("Pending", "Failed", "Dead Letter"):
		out.append(("erp_outbox_messages", "Integration outbox messages by status", {"status": status},
			float(_count("Integration Outbound Message", {"status": status}))))
	for status in ("Failed", "Dead Letter", "Rejected"):
		out.append(("erp_inbox_messages", "Integration inbox messages by status", {"status": status},
			float(_count("Integration Inbound Message", {"status": status}))))
	out.append(("erp_audit_unsealed_events", "Audit events not yet sealed into the hash chain", {},
		float(cint(frappe.db.sql("select count(*) from `tabAudit Event` where ifnull(chain_index, 0) = 0")[0][0]))))
	out.append(("erp_approvals_pending", "Approval requests awaiting a decision", {},
		float(_count("Approval Request", {"status": "Pending"}))))
	out.append(("erp_error_logs_24h", "Error Log entries in the last 24 hours", {},
		float(_count("Error Log", {"creation": (">", frappe.utils.add_to_date(now_datetime(), hours=-24))}))))
	for path in hook_list("automotive_metrics"):  # app-specific work queues (holds, andon, kanban, ...)
		out.extend(frappe.get_attr(path)())
	marker = Path(frappe.get_site_path(*BACKUP_MARKER))
	if marker.exists():
		last = get_datetime(marker.read_text().strip())
		out.append(("erp_backup_last_success_timestamp_seconds", "Unix time of the last verified backup", {},
			float(last.timestamp())))
	last_scheduled = frappe.db.sql("select max(creation) from `tabScheduled Job Log` where status = 'Complete'")[0][0]
	if last_scheduled:
		out.append(("erp_scheduler_last_job_timestamp_seconds", "Unix time of the last completed scheduled job", {},
			float(get_datetime(last_scheduled).timestamp())))
	for metric in out:
		metric[2].setdefault("site", site)
	return out


def render(metrics: list[tuple]) -> str:
	"""Prometheus text format. Entries are (name, help, labels, value) gauges, or carry a
	fifth element with the type; histogram samples (_bucket/_sum/_count) share one header."""
	lines: list[str] = []
	seen: set[str] = set()
	for name, help_text, labels, value, *kind in metrics:
		kind = kind[0] if kind else "gauge"
		family = name
		if kind == "histogram":
			for suffix in ("_bucket", "_sum", "_count"):
				if name.endswith(suffix):
					family = name[: -len(suffix)]
		if family not in seen:
			lines += [f"# HELP {family} {help_text}", f"# TYPE {family} {kind}"]
			seen.add(family)
		label_text = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
		lines.append(f"{name}{{{label_text}}} {value:g}")
	return "\n".join(lines) + "\n"


def _escape(value) -> str:
	return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


@frappe.whitelist(allow_guest=True, methods=["GET"])
def metrics():
	if not _authorised():
		return _respond({"status": "forbidden"}, 403)
	frappe.local.response.filename = "metrics.txt"
	frappe.local.response.filecontent = render(collect())
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "inline"
	return None
