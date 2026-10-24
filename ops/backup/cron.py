#!/usr/bin/env python3
"""The stack's scheduled operations jobs, run inside the stack without the Docker socket
(the `cron` service of infra/deploy/compose.hinode.yaml; ops/security/findings.md C-02).

Jobs and their schedules (environment, cron with seconds first, UTC):

	BACKUP_SCHEDULE          default "0 30 17 * * *"   bash ops/backup/backup.sh
	RESTORE_DRILL_SCHEDULE   default "0 30 19 * * 6"   bash ops/backup/restore-drill.sh

The service runs the application image with the scheduler's environment and secrets, so
the jobs see exactly what they would see in the scheduler container. A run missed while
the service was down is not caught up: the next one comes at its schedule. One job runs
at a time; output goes to the service log with the job name and exit status.

Uses croniter, which Frappe itself depends on (the bench's Python).
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import subprocess
import sys
import time

from croniter import croniter

JOBS = {
	"backup": ("BACKUP_SCHEDULE", "0 30 17 * * *", ["bash", "/opt/hinode/ops/backup/backup.sh"]),
	"restore-drill": ("RESTORE_DRILL_SCHEDULE", "0 30 19 * * 6", ["bash", "/opt/hinode/ops/backup/restore-drill.sh"]),
}


def log(message: str) -> None:
	print(f"{dt.datetime.now(dt.UTC):%Y-%m-%dT%H:%M:%SZ} [cron] {message}", flush=True)


def to_croniter(expression: str) -> str:
	"""Seconds-first (sec min hour dom month dow, the stack's documented format) to
	croniter's order, which takes seconds as the sixth field."""
	fields = expression.split()
	if len(fields) != 6:
		raise ValueError(f"expected six fields (seconds first): {expression!r}")
	return " ".join([*fields[1:], fields[0]])


def schedules(now: dt.datetime) -> dict[str, croniter]:
	return {name: croniter(to_croniter(os.environ.get(variable) or default), now)
		for name, (variable, default, _command) in JOBS.items()}


def main() -> int:
	signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
	timers = schedules(dt.datetime.now(dt.UTC).replace(tzinfo=None))
	due = {name: timer.get_next(dt.datetime) for name, timer in timers.items()}
	for name, at in sorted(due.items()):
		log(f"{name}: {os.environ.get(JOBS[name][0]) or JOBS[name][1]} (UTC), next at {at:%Y-%m-%d %H:%M:%S}")
	while True:
		name = min(due, key=due.get)
		wait = (due[name] - dt.datetime.now(dt.UTC).replace(tzinfo=None)).total_seconds()
		if wait > 0:
			time.sleep(min(wait, 60))
			continue
		log(f"{name}: starting")
		started = time.monotonic()
		status = subprocess.call(JOBS[name][2], cwd="/home/frappe/frappe-bench")  # noqa: S603 (fixed commands)
		log(f"{name}: exit status {status} after {time.monotonic() - started:.0f} s")
		due[name] = timers[name].get_next(dt.datetime)


if __name__ == "__main__":
	sys.exit(main())
