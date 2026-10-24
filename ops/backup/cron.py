#!/usr/bin/env python3
"""The stack's scheduled operations jobs, run inside the stack without the Docker socket
(the `cron` service of infra/deploy/compose.hinode.yaml; ops/security/findings.md C-02).

Jobs and their schedules (environment, cron with seconds first, UTC; "off" disables a job):

	BACKUP_SCHEDULE          default "0 30 17 * * *"   bash ops/backup/backup.sh
	RESTORE_DRILL_SCHEDULE   default "0 30 19 * * 6"   bash ops/backup/restore-drill.sh
	BINLOG_SCHEDULE          default "0 */5 * * * *"   ops/backup/binlog.py ship

The service runs the application image with the scheduler's environment and secrets, so
the jobs see exactly what they would see in the scheduler container. Each lane runs one job
at a time: the backup and the drill share one, and the binary log shipping has its own, so
the recovery point keeps moving while a backup or drill runs. A run missed (the service was
down, or the lane busy) is not caught up: the next one comes at its schedule. Output goes to
the service log with the job name and exit status.

Uses croniter, which Frappe itself depends on (the bench's Python).
"""

from __future__ import annotations

import datetime as dt
import os
import signal
import subprocess
import sys
import threading
import time

from croniter import croniter

PYTHON = "/home/frappe/frappe-bench/env/bin/python"
JOBS = {
	"backup": (
		"maintenance",
		"BACKUP_SCHEDULE",
		"0 30 17 * * *",
		["bash", "/opt/hinode/ops/backup/backup.sh"],
	),
	"restore-drill": (
		"maintenance",
		"RESTORE_DRILL_SCHEDULE",
		"0 30 19 * * 6",
		["bash", "/opt/hinode/ops/backup/restore-drill.sh"],
	),
	"binlog": (
		"binlog",
		"BINLOG_SCHEDULE",
		"0 */5 * * * *",
		[PYTHON, "/opt/hinode/ops/backup/binlog.py", "ship"],
	),
}


def log(message: str) -> None:
	print(f"{dt.datetime.now(dt.UTC):%Y-%m-%dT%H:%M:%SZ} [cron] {message}", flush=True)


def utcnow() -> dt.datetime:
	return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def to_croniter(expression: str) -> str:
	"""Seconds-first (sec min hour dom month dow, the stack's documented format) to
	croniter's order, which takes seconds as the sixth field."""
	fields = expression.split()
	if len(fields) != 6:
		raise ValueError(f"expected six fields (seconds first): {expression!r}")
	return " ".join([*fields[1:], fields[0]])


def schedule(name: str) -> str | None:
	_lane, variable, default, _command = JOBS[name]
	expression = (os.environ.get(variable) or default).strip()
	return None if expression.lower() == "off" else to_croniter(expression)


def next_run(expression: str) -> dt.datetime:
	"""The first time after now: runs missed while the lane was busy are skipped."""
	return croniter(expression, utcnow()).get_next(dt.datetime)


def lane(jobs: list[str]) -> None:
	due = {name: next_run(schedule(name)) for name in jobs}
	while True:
		name = min(due, key=due.get)
		wait = (due[name] - utcnow()).total_seconds()
		if wait > 0:
			time.sleep(min(wait, 60))
			continue
		log(f"{name}: starting")
		started = time.monotonic()
		status = subprocess.call(JOBS[name][3], cwd="/home/frappe/frappe-bench")  # noqa: S603 (fixed commands)
		log(f"{name}: exit status {status} after {time.monotonic() - started:.0f} s")
		due[name] = next_run(schedule(name))


def main() -> int:
	signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
	lanes: dict[str, list[str]] = {}
	for name, (lane_name, variable, default, _command) in JOBS.items():
		expression = schedule(name)
		if expression is None:
			log(f"{name}: off ({variable})")
			continue
		log(
			f"{name}: {os.environ.get(variable) or default} (UTC), next at {next_run(expression):%Y-%m-%d %H:%M:%S}"
		)
		lanes.setdefault(lane_name, []).append(name)
	threads = [
		threading.Thread(target=lane, args=(jobs,), name=lane_name, daemon=True)
		for lane_name, jobs in lanes.items()
	]
	for thread in threads:
		thread.start()
	# idle when every job is off (the restart policy would otherwise restart the service in a
	# loop); a lane that dies ends the service, which the restart policy then restarts
	while all(thread.is_alive() for thread in threads):
		time.sleep(60)
	log("a job lane stopped")
	return 1


if __name__ == "__main__":
	sys.exit(main())
