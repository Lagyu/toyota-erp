"""Seed run context: deterministic randomness, a monotonic simulated clock under frozen
time, document counters and commit/resume handling.

Determinism: every random decision draws from a named stream
(``ctx.rng("dealer-orders")``) seeded from (profile seed, stream name, simulated day),
so the same profile and seed always produce the same decisions in the same order, and
resuming a day reproduces exactly what an uninterrupted run would have done.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

import frappe
import yaml
from frappe.utils import add_days, getdate

PROFILE_DIR = Path(__file__).resolve().parent.parent / "profiles"


def load_profile(name: str) -> dict[str, Any]:
	path = PROFILE_DIR / f"{name}.yaml"
	if not path.exists():
		frappe.throw(f"Unknown seed profile {name}")
	return yaml.safe_load(path.read_text())


class Clock:
	"""Simulated wall clock: frozen per step, strictly increasing within a day so stock
	postings keep their order."""

	def __init__(self, day: dt.date):
		self.day = day
		self.now = dt.datetime.combine(day, dt.time(6, 0))

	def at(self, hour: int, minute: int = 0) -> dt.datetime:
		target = dt.datetime.combine(self.day, dt.time(hour, minute))
		self.now = max(self.now + dt.timedelta(seconds=7), target)
		return self.now

	def tick(self, seconds: int = 30) -> dt.datetime:
		self.now = self.now + dt.timedelta(seconds=seconds)
		end = dt.datetime.combine(self.day, dt.time(23, 59, 0))
		if self.now > end:
			self.now = end
		return self.now


class SeedContext:
	def __init__(self, run_name: str, profile: dict[str, Any]):
		self.run_name = run_name
		self.profile = profile
		self.seed = int(profile["seed"])
		self.start = getdate(profile["start_date"])
		self.end = add_days(self.start, round(float(profile["months"]) * 30.4) - 1)
		self.counts: Counter = Counter()
		self.cache: dict[str, Any] = {}
		self.clock: Clock | None = None
		self._freezers: list = []
		self._latest: dt.datetime | None = None

	# ---------------------------------------------------------------------------------
	def rng(self, stream: str, day: dt.date | None = None) -> random.Random:
		key = f"{self.seed}:{stream}:{day.isoformat() if day else '-'}"
		return random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))

	def v(self, key: str, default=None):
		return self.profile.get("volume", {}).get(key, default)

	# ---------------------------------------------------------------------------------
	@contextlib.contextmanager
	def frozen(self, moment: dt.datetime):
		"""Freeze time at ``moment`` (system time zone). Simulated time never runs backwards:
		a nested block starts no earlier than the enclosing one, and when it ends the
		enclosing block continues from the latest simulated time instead of jumping back
		(otherwise a receipt booked inside a nested step would post *after* the build that
		consumes it)."""
		from freezegun import freeze_time

		if self._freezers:
			moment = max(moment, self._latest)
		self._latest = max(self._latest, moment) if self._latest else moment
		with freeze_time(_utc(moment)) as factory:
			self._freezers.append(factory)
			try:
				yield
			finally:
				self._freezers.pop()
		if self._freezers:
			latest = max(self._latest, self.clock.now) if self.clock else self._latest
			self._latest = latest
			self._freezers[-1].move_to(_utc(latest))

	@contextlib.contextmanager
	def step(self, hour: int, minute: int = 0):
		"""Freeze time at the next clock tick after hour:minute of the simulated day."""
		moment = self.clock.at(hour, minute)
		with self.frozen(moment):
			yield moment

	def count(self, doctype: str, n: int = 1) -> None:
		self.counts[doctype] += n

	def as_user(self, user: str):
		return _as_user(user)

	def save_progress(self, **values) -> None:
		values["counts"] = json.dumps(dict(sorted(self.counts.items())))
		frappe.db.set_value("Seed Run", self.run_name, values, update_modified=False)


def _utc(moment: dt.datetime) -> dt.datetime:
	from zoneinfo import ZoneInfo

	from frappe.utils.data import get_datetime, get_system_timezone

	return get_datetime(moment).replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(dt.UTC)


@contextlib.contextmanager
def _as_user(user: str):
	previous = frappe.session.user
	frappe.set_user(user)
	try:
		yield
	finally:
		frappe.set_user(previous)


def working_day(day: dt.date, plant: str) -> bool:
	from automotive_manufacturing.production_planning.snapshot import working_days

	return day.isoformat() in set(working_days(plant, day, day))
