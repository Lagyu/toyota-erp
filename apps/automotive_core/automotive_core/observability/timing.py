"""Server time of HTTP requests for latency alerting (§6.2 "degraded latency", §7 targets).

Every request (except static files and the health endpoints) adds its duration to a
cumulative histogram kept in Redis, shared by all web workers of the site, by kind (api,
desk, portal) and verb (read = GET/HEAD, write = everything else). The metrics endpoint
exposes it as ``erp_http_request_duration_seconds``; Prometheus derives p95 per kind and
the alert rules compare it with the brief's targets (reads ≤ 2 s, form submissions ≤ 4 s).
Counters only grow; a cache flush looks like a counter reset, which ``rate()`` handles.
"""

from __future__ import annotations

import contextlib
import time

import frappe

BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
KINDS = ("api", "desk", "portal")
VERBS = ("read", "write")
NAME = "erp_http_request_duration_seconds"
HELP = "Server time of HTTP requests by kind and verb"
_PREFIX = "automotive:http_duration"
_SKIP = ("/assets/", "/files/", "/private/files/", "/socket.io", "/api/method/automotive_core.ops.health.")


def _base(kind: str, verb: str) -> str:
	return frappe.cache.make_key(f"{_PREFIX}:{kind}:{verb}")


def before_request() -> None:
	frappe.local.automotive_request_started = time.perf_counter()


def after_request(response=None, request=None) -> None:
	started = getattr(frappe.local, "automotive_request_started", None)
	request = request if request is not None else getattr(frappe.local, "request", None)
	if started is None or request is None:
		return
	path = request.path or ""
	if path.startswith(_SKIP):
		return
	observe(time.perf_counter() - started, path, request.method or "GET")


def observe(duration: float, path: str, method: str) -> None:
	kind = "api" if path.startswith("/api/") else "desk" if path.startswith(("/desk", "/app")) else "portal"
	verb = "read" if method.upper() in ("GET", "HEAD") else "write"
	base = _base(kind, verb)
	# timing must never break a request: a cache hiccup only loses this observation
	with contextlib.suppress(Exception):
		pipe = frappe.cache.pipeline()
		for le in BUCKETS:
			if duration <= le:
				pipe.incr(f"{base}:le:{le:g}")
		pipe.incr(f"{base}:count")
		pipe.incrbyfloat(f"{base}:sum", duration)
		pipe.execute()


def metrics() -> list[tuple]:
	"""Histogram samples for automotive_core.ops.health.metrics (hook automotive_metrics)."""
	out: list[tuple] = []
	for kind in KINDS:
		for verb in VERBS:
			base = _base(kind, verb)
			keys = [f"{base}:le:{le:g}" for le in BUCKETS] + [f"{base}:count", f"{base}:sum"]
			values = [float(v.decode() if isinstance(v, bytes) else v) if v is not None else 0.0
				for v in frappe.cache.mget(keys)]
			labels = {"kind": kind, "verb": verb}
			for le, value in zip(BUCKETS, values, strict=False):
				out.append((f"{NAME}_bucket", HELP, {**labels, "le": f"{le:g}"}, value, "histogram"))
			out.append((f"{NAME}_bucket", HELP, {**labels, "le": "+Inf"}, values[-2], "histogram"))
			out.append((f"{NAME}_sum", HELP, dict(labels), values[-1], "histogram"))
			out.append((f"{NAME}_count", HELP, dict(labels), values[-2], "histogram"))
	return out
