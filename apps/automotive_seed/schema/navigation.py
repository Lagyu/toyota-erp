"""Desk navigation for automotive_seed (non-production sites only)."""

from desk import Link, Workspace
from roles import SYSTEM

DESK = [
	Workspace(
		"Reference Data",
		"Reference Data",
		"database",
		[SYSTEM],
		sequence=99,
		groups=[],
		sidebar=[Link("Seed Run", "Seed Run", icon="sprout")],
	),
]
