"""Mixed-model line sequencing (heijunka) by goal chasing. Pure functions.

For a day's build list with N units of mix keys m (e.g. model/drivetrain) with
demand d_m, the ideal cumulative quantity of m after position k is k*d_m/N. Goal
chasing picks, at each position, the key furthest behind its ideal. Ties break on
remaining demand, then on the key itself, so results are reproducible.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class Unit:
	ref: str  # work order (and unit index) reference
	mix_key: str


def goal_chasing(units: list[Unit]) -> list[Unit]:
	n = len(units)
	if n == 0:
		return []
	demand = Counter(u.mix_key for u in units)
	pools: dict[str, list[Unit]] = {}
	for unit in sorted(units, key=lambda u: (u.mix_key, u.ref)):
		pools.setdefault(unit.mix_key, []).append(unit)
	placed: Counter = Counter()
	sequence: list[Unit] = []
	for k in range(1, n + 1):
		best = None
		best_score = None
		for key in sorted(pools):
			if not pools[key]:
				continue
			gap = k * demand[key] / n - placed[key]
			remaining = len(pools[key])
			score = (gap, remaining, [-ord(c) for c in key])
			if best_score is None or score > best_score:
				best, best_score = key, score
		sequence.append(pools[best].pop(0))
		placed[best] += 1
	return sequence


def leveling_deviation(sequence: list[Unit]) -> float:
	"""Sum over positions and keys of squared deviation from the ideal cumulative mix."""
	n = len(sequence)
	if n == 0:
		return 0.0
	demand = Counter(u.mix_key for u in sequence)
	placed: Counter = Counter()
	total = 0.0
	for k, unit in enumerate(sequence, start=1):
		placed[unit.mix_key] += 1
		for key, d in demand.items():
			total += (k * d / n - placed[key]) ** 2
	return round(total, 6)
