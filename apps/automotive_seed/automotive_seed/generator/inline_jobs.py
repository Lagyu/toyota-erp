"""Run background jobs inline while synthetic history is generated.

Frappe and ERPNext enqueue follow-up work (contacts for new users, dynamic-link cleanup,
large document submits via ``queue_action``). A generator that left those in Redis would
(a) overflow the queue guard on a bench without workers and (b) have them executed later,
outside the frozen simulated clock and outside the day's transaction. Instead every job
runs immediately in the generating process, exactly like ``enqueue(..., now=True)``.

Jobs that only maintain caches unrelated to business data (website search index) are
skipped; they are rebuilt by the scheduler once the site is in use.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import frappe
import frappe.utils.background_jobs as background_jobs

SKIPPED = frozenset({
	"frappe.search.website_search.build_index_for_all_routes",
	"frappe.website.doctype.website_route_meta.website_route_meta.build_index_for_all_routes",
})


def _method_name(method: str | Callable) -> str:
	if isinstance(method, str):
		return method
	return f"{method.__module__}.{method.__qualname__}"


def _inline(method: str | Callable, queue: str = "default", timeout: int | None = None, event: str | None = None,
		is_async: bool = True, job_name: str | None = None, now: bool = False, enqueue_after_commit: bool = False,
		*, on_success: Callable | None = None, on_failure: Callable | None = None, at_front: bool = False,
		job_id: str | None = None, deduplicate: bool = False, at_front_when_starved: bool = False,
		**kwargs: Any) -> Any:
	kwargs.pop("async", None)
	name = _method_name(method)
	if name in SKIPPED or name.endswith(".build_index_for_all_routes"):
		return None
	frappe.local.flags.setdefault("automotive_inline_jobs", {})
	counts = frappe.local.flags.automotive_inline_jobs
	counts[name] = counts.get(name, 0) + 1
	result = frappe.call(method, **kwargs)
	if on_success:
		on_success(None, None, result)
	return result


@contextmanager
def inline_background_jobs() -> Iterator[None]:
	original = background_jobs.enqueue
	patched: list[tuple[object, str]] = []
	for module in list(sys.modules.values()):
		if module is None:
			continue
		for attr in ("enqueue",):
			try:
				if getattr(module, attr, None) is original:
					setattr(module, attr, _inline)
					patched.append((module, attr))
			except Exception:  # noqa: S112 - exotic modules that refuse attribute writes
				continue
	try:
		yield
	finally:
		for module, attr in patched:
			setattr(module, attr, original)
