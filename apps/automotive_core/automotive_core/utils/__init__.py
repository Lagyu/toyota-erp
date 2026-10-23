"""Small shared helpers (no business logic)."""

from __future__ import annotations

import datetime as _dt
import decimal
import hashlib
import json
from typing import Any

import frappe


def _default(value: Any) -> Any:
	if isinstance(value, _dt.datetime):
		return value.isoformat(sep=" ")
	if isinstance(value, _dt.date | _dt.time | _dt.timedelta):
		return str(value)
	if isinstance(value, decimal.Decimal):
		return str(value)
	if isinstance(value, set | frozenset):
		return sorted(value)
	raise TypeError(f"not JSON serialisable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
	"""Deterministic JSON (sorted keys, no whitespace) used for hashing and signing."""
	return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default)


def sha256_hex(data: str | bytes) -> str:
	if isinstance(data, str):
		data = data.encode()
	return hashlib.sha256(data).hexdigest()


def hook_list(name: str) -> list[str]:
	"""All values of a list-valued custom hook across installed apps, de-duplicated, ordered."""
	seen: dict[str, None] = {}
	for value in frappe.get_hooks(name) or []:
		if isinstance(value, list | tuple):
			for item in value:
				seen.setdefault(item, None)
		else:
			seen.setdefault(value, None)
	return list(seen)


def hook_set(name: str) -> frozenset[str]:
	"""Cached per request (frappe.local) because the wildcard doc hooks call this on every save."""
	cache = local_cache("automotive_hook_sets")
	if name not in cache:
		cache[name] = frozenset(hook_list(name))
	return cache[name]


def local_cache(key: str) -> dict:
	"""A dict that lives for the current request/job (frappe.local is reset per request)."""
	cache = getattr(frappe.local, key, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, key, cache)
	return cache


def hook_dict(name: str) -> dict[str, str]:
	"""Merge a dict-valued custom hook (frappe.get_hooks returns {key: [value per app]}).

	Two apps registering different targets for the same key is a configuration error,
	not something to resolve silently by load order.
	"""
	merged: dict[str, str] = {}
	for key, targets in (frappe.get_hooks(name) or {}).items():
		targets = targets if isinstance(targets, list | tuple) else [targets]
		unique = list(dict.fromkeys(targets))
		if len(unique) > 1:
			frappe.throw(f"Hook {name}: key {key} registered by several apps: {unique}")
		merged[key] = unique[0]
	return merged


def is_seed_or_install() -> bool:
	return bool(frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_patch)


def commit() -> None:
	"""Commit a scheduled/background unit of work. Tests run inside a transaction that
	IntegrationTestCase rolls back, so committing there would leak test data."""
	if not frappe.flags.in_test:
		frappe.db.commit()
