#!/usr/bin/env python3
"""Binary logs between the nightly backups: shipped off the host every few minutes and
replayed onto a restored backup (point-in-time recovery; ops/runbooks/backup-restore.md).

	binlog.py ship                      the cron job (BINLOG_SCHEDULE, every 5 minutes)
	binlog.py available                 exit 0 when the server writes a binary log
	binlog.py position DUMP OUTPUT      the binary log position recorded in a dump
	binlog.py replay POSITION LOGS DATABASE [--source DATABASE] [--until TIME] [--report FILE]
	                                    apply the logs in directory LOGS from POSITION (a backup
	                                    set's -binlog.json) to DATABASE; TIME in UTC

Runs in the scheduler and cron containers with the bench's Python (PyMySQL) as user frappe.
The database root password comes from DB_ROOT_PASSWORD_FILE and reaches the MariaDB tools
in an owner-only option file, never on a command line.

Shipping: a heartbeat row is written, the current binary log is closed (FLUSH BINARY LOGS)
and each closed log not yet copied is read from the server (mariadb-binlog
--read-from-remote-server --raw) into a local mirror, which restic stores off the host
(tag binlog:<site>). The mirror holds the logs from the oldest position of the last
KEEP_BACKUPS backups, so the newest binlog snapshot alone can bring any of them forward.
The site's public and private files follow (tag files:<site>; restic stores only what is
new): the binary logs hold the File records, not the files. A recovery point is thus at
most one interval (plus the copy) behind.

Position: bench backup's dump runs through bin/mariadb-dump, which adds --master-data=2:
with --single-transaction MariaDB writes the binary log position of the dump's own snapshot
into the dump (a comment), without locking. Replaying from it applies each later change
exactly once: the dump holds everything committed before it, the logs everything after.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

BENCH = Path(os.environ.get("BENCH_DIR") or "/home/frappe/frappe-bench")
KEEP_BACKUPS = 3
# the image's tools, by absolute path (the commands below are fixed: no user input reaches them)
RESTIC, MARIADB, MARIADB_BINLOG = "/usr/bin/restic", "/usr/bin/mariadb", "/usr/bin/mariadb-binlog"
HEARTBEAT = "__hinode_heartbeat"
POSITION = re.compile(r"CHANGE MASTER TO MASTER_LOG_FILE='([^']+)', MASTER_LOG_POS=(\d+)")
GTID = re.compile(r"SET GLOBAL gtid_slave_pos='([^']*)'")


def log(message: str) -> None:
	print(f"{dt.datetime.now(dt.UTC):%Y-%m-%dT%H:%M:%SZ} [binlog] {message}", flush=True)


def now() -> str:
	return f"{dt.datetime.now(dt.UTC):%Y-%m-%dT%H:%M:%SZ}"


def site_name() -> str:
	return os.environ["SITE_NAME"]


def site_db(site: str) -> str:
	return json.loads((BENCH / "sites" / site / "site_config.json").read_text())["db_name"]


def state_dir(site: str) -> Path:
	return BENCH / "sites" / site / "private" / "ops" / "binlog"


def server() -> dict:
	common = json.loads((BENCH / "sites" / "common_site_config.json").read_text())
	return {
		"host": common.get("db_host") or "db",
		"port": int(common.get("db_port") or 3306),
		"user": os.environ.get("DB_ROOT_USER") or "root",
		"password": Path(os.environ["DB_ROOT_PASSWORD_FILE"]).read_text().strip(),
	}


def connect(database: str | None = None):
	import pymysql

	return pymysql.connect(**server(), database=database, autocommit=True)


@contextlib.contextmanager
def client_options():
	"""A [client] option file for mariadb and mariadb-binlog: owner-only, removed afterwards."""
	conf = server()
	quoted = conf["password"].replace("\\", "\\\\").replace('"', '\\"')
	fd, path = tempfile.mkstemp(prefix="binlog-", suffix=".cnf")
	try:
		with os.fdopen(fd, "w") as handle:
			handle.write(
				f'[client]\nhost={conf["host"]}\nport={conf["port"]}\nuser={conf["user"]}\npassword="{quoted}"\n'
			)
		yield path
	finally:
		os.unlink(path)


def index(name: str) -> int:
	return int(name.rsplit(".", 1)[1])


def restic(*args: str, ok: tuple[int, ...] = (0,)) -> None:
	# a nightly prune holds the repository exclusively: wait for it rather than fail
	status = subprocess.run([RESTIC, "--retry-lock", "10m", *args], check=False).returncode  # noqa: S603
	if status not in ok:
		raise subprocess.CalledProcessError(status, [RESTIC, *args])


def available() -> int:
	try:
		with connect() as conn, conn.cursor() as cur:
			cur.execute("SELECT @@log_bin")
			return 0 if cur.fetchone()[0] else 1
	except Exception as exc:  # no root access, server down: the backup goes on without a position
		log(f"binary log not available: {exc}")
		return 1


def _keep_from(state: Path, closed: list[str]) -> int:
	"""The oldest log the mirror keeps: that of the oldest of the last backups' positions,
	or (before any backup has one) the newest closed log."""
	positions = sorted((state / "positions").glob("*.json"))[-KEEP_BACKUPS:]
	files = [json.loads(p.read_text())["file"] for p in positions]
	if files:
		return min(index(f) for f in files)
	return index(closed[-1]) if closed else sys.maxsize


def ship() -> int:
	site = site_name()
	state = state_dir(site)
	mirror = state / "logs"
	mirror.mkdir(parents=True, exist_ok=True)
	with connect(site_db(site)) as conn, conn.cursor() as cur:
		cur.execute("SELECT @@log_bin")
		if not cur.fetchone()[0]:
			log("the database server writes no binary log: nothing to ship")
			return 1
		# the newest change a replay can recover: a drill reads it back to measure the RPO
		cur.execute(
			f"CREATE TABLE IF NOT EXISTS `{HEARTBEAT}` (id TINYINT PRIMARY KEY, beat DATETIME(6) NOT NULL)"
		)
		cur.execute(f"REPLACE INTO `{HEARTBEAT}` VALUES (1, UTC_TIMESTAMP(6))")  # noqa: S608 (a constant)
		cur.execute(f"SELECT beat FROM `{HEARTBEAT}` WHERE id = 1")  # noqa: S608
		beat = cur.fetchone()[0]
		cur.execute("FLUSH BINARY LOGS")
		cur.execute("SHOW BINARY LOGS")
		closed = [row[0] for row in cur.fetchall()][:-1]  # the last one is the new, open log
	start = _keep_from(state, closed)
	wanted = [name for name in closed if index(name) >= start and not (mirror / name).exists()]
	if wanted:
		with client_options() as options, tempfile.TemporaryDirectory(dir=state) as tmp:
			for name in wanted:
				subprocess.run(  # noqa: S603
					[
						MARIADB_BINLOG,
						f"--defaults-extra-file={options}",
						"--read-from-remote-server",
						"--raw",
						f"--result-file={tmp}/",
						name,
					],
					check=True,
				)
				os.replace(Path(tmp, name), mirror / name)
	for path in mirror.iterdir():
		if index(path.name) < start:
			path.unlink()
	held = sorted(mirror.iterdir(), key=lambda p: index(p.name))
	restic("backup", "--quiet", "--host", site, "--tag", f"binlog:{site}", str(mirror))
	# the files of the records in these logs: written before their records were committed, so
	# a snapshot taken after the flush holds the file of every record a replay brings back
	site_dir = BENCH / "sites" / site
	restic(
		"backup",
		"--quiet",
		"--host",
		site,
		"--tag",
		f"files:{site}",
		str(site_dir / "public" / "files"),
		str(site_dir / "private" / "files"),
		ok=(0, 3),
	)  # 3: a file removed during the scan; the snapshot holds the rest
	(state / "last-shipped.json").write_text(
		json.dumps(
			{
				"shipped_at": now(),
				"heartbeat": f"{beat:%Y-%m-%dT%H:%M:%S.%fZ}",
				"logs": len(held),
				"first": held[0].name if held else None,
				"last": held[-1].name if held else None,
			}
		)
	)
	log(
		f"shipped {len(wanted)} log(s) and the files; the mirror holds {len(held)}"
		+ (f" ({held[0].name} to {held[-1].name})" if held else "")
	)
	return 0


def position(dump: str, output: str) -> int:
	"""The position bin/mariadb-dump wrote into the dump, as the backup set's -binlog.json
	(and a copy in the state directory, from which the mirror's retention is decided)."""
	import gzip

	found = gtid = None
	with gzip.open(dump, "rt", errors="replace") as handle:
		for _n, line in zip(range(400), handle, strict=False):
			found = found or POSITION.search(line)
			gtid = gtid or GTID.search(line)
			if found and gtid:
				break
	if not found:
		log(f"no binary log position in {Path(dump).name}: this backup cannot be brought forward")
		return 1
	record = {
		"file": found.group(1),
		"position": int(found.group(2)),
		"gtid": gtid.group(1) if gtid else None,
		"dump": Path(dump).name,
		"recorded_at": now(),
	}
	Path(output).write_text(json.dumps(record))
	positions = state_dir(site_name()) / "positions"
	positions.mkdir(parents=True, exist_ok=True)
	(positions / f"{Path(dump).name.removesuffix('-database.sql.gz')}.json").write_text(json.dumps(record))
	log(f"backup position {record['file']}:{record['position']}")
	return 0


class LogsMissing(Exception):
	pass


def logs_from(first: str, logs: Path) -> list[Path]:
	"""The binary log files under ``logs`` from ``first`` on, in order, without gaps."""
	files = sorted(
		(
			p
			for p in logs.rglob("*")
			if p.is_file() and re.search(r"\.\d+$", p.name) and index(p.name) >= index(first)
		),
		key=lambda p: index(p.name),
	)
	if not files or files[0].name != first:
		raise LogsMissing(f"the shipped logs do not reach back to {first}")
	gaps = [f"{a.name}..{b.name}" for a, b in itertools.pairwise(files) if index(b.name) != index(a.name) + 1]
	if gaps:
		raise LogsMissing(f"shipped logs are missing between {', '.join(gaps)}")
	return files


def replay(
	position_file: str, logs: str, database: str, source: str | None, until: str | None, report: str | None
) -> int:
	"""Apply the shipped logs from the backup's position (rows of the source database only,
	renamed to ``database``), up to ``until`` or their end. Not binlogged again."""
	start = json.loads(Path(position_file).read_text())
	try:
		files = logs_from(start["file"], Path(logs))
	except LogsMissing as exc:
		log(str(exc))
		return 2
	source = source or database
	# MariaDB filters on the database name as logged, then --rewrite-db renames it
	command = [MARIADB_BINLOG, f"--start-position={start['position']}", f"--database={source}"]
	if source != database:
		command.append(f"--rewrite-db={source}->{database}")
	if until:
		command.append(f"--stop-datetime={until}")
	command.extend(str(f) for f in files)
	with client_options() as options:
		reader = subprocess.Popen(command, stdout=subprocess.PIPE, env={**os.environ, "TZ": "UTC"})  # noqa: S603
		applied = subprocess.run(  # noqa: S603
			[
				MARIADB,
				f"--defaults-extra-file={options}",
				"--init-command=SET sql_log_bin=0",
				database,
			],
			stdin=reader.stdout,
			check=False,
		)
		reader.stdout.close()
		status = reader.wait()
	if status or applied.returncode:
		log(f"replay failed (mariadb-binlog {status}, mariadb {applied.returncode})")
		return 1
	with connect(database) as conn, conn.cursor() as cur:
		cur.execute(
			"SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
			(database, HEARTBEAT),
		)
		beat = None
		if cur.fetchone()[0]:
			cur.execute(f"SELECT beat FROM `{HEARTBEAT}` WHERE id = 1")  # noqa: S608
			row = cur.fetchone()
			beat = f"{row[0]:%Y-%m-%dT%H:%M:%S.%fZ}" if row else None
	result = {
		"from": f"{start['file']}:{start['position']}",
		"logs": len(files),
		"last_log": files[-1].name,
		"until": until,
		"recovered_to": beat,
	}
	if report:
		Path(report).write_text(json.dumps(result))
	log(f"replayed {len(files)} log(s) from {result['from']}; newest heartbeat {beat}")
	return 0


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	sub = parser.add_subparsers(dest="command", required=True)
	sub.add_parser("ship")
	sub.add_parser("available")
	p = sub.add_parser("position")
	p.add_argument("dump")
	p.add_argument("output")
	r = sub.add_parser("replay")
	r.add_argument("position")
	r.add_argument("logs")
	r.add_argument("database")
	r.add_argument("--source")
	r.add_argument("--until")
	r.add_argument("--report")
	args = parser.parse_args()
	if args.command == "ship":
		return ship()
	if args.command == "available":
		return available()
	if args.command == "position":
		return position(args.dump, args.output)
	return replay(args.position, args.logs, args.database, args.source, args.until, args.report)


if __name__ == "__main__":
	sys.exit(main())
