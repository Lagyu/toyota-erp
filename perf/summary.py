#!/usr/bin/env python3
"""Results of a load-test run (scripts/perf/run.sh, perf/locustfile.py): p50/p95/p99 per
request group and operation, checked against the p95 targets of PRF-3.

	summary.py RESULTS_DIR [--read-p95 SECONDS] [--write-p95 SECONDS] [--max-fail-ratio RATIO]
	summary.py scrape URL TOKEN_FILE OUTPUT

The first form reads RESULTS_DIR/stats.json (Locust --json-file), the Locust exceptions
CSV and, when present, the metrics snapshots metrics-before.prom / metrics-after.prom;
prints the tables and writes summary.txt and summary.json next to them. Exit status 0:
targets met; 1: a p95 target missed, failures above the ratio, or errors inside the test;
2: no usable results.

The second form saves the site's Prometheus metrics (automotive_core.ops.health.metrics)
to OUTPUT with the token read from TOKEN_FILE (header X-Hinode-Metrics-Token). The token is never printed.

	summary.py sample URL TOKEN_FILE OUTPUT [SECONDS]
	summary.py slowlog SLOW_LOG OUTPUT_JSON

sample appends the RQ queue lengths (erp_queue_jobs) to OUTPUT every SECONDS (default 15)
until it is stopped; slowlog digests a MariaDB slow query log into statements grouped by their
shape (literals masked), with their count and time. When RESULTS_DIR holds them, the report
adds: throughput; the queue lengths sampled during the phase (queues.txt); the slow statements
(slow-queries.json); the database and file storage before and after the phase (db-before.json,
db-after.json, files-before.txt, files-after.txt).

Groups follow the request names of the locustfile: read:* (GET), write:* (other methods),
job:* (completion times of background jobs, no target). Client-side times include the
network; the server histogram (erp_http_request_duration_seconds) does not.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

PERCENTILES = (0.5, 0.95, 0.99)
HISTOGRAM = "erp_http_request_duration_seconds"
SAMPLE = re.compile(r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>\S+)")
LABEL = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


# ---------------------------------------------------------------------------------------
# Client side (Locust)
# ---------------------------------------------------------------------------------------


def percentile(times: Counter, count: int, share: float) -> int:
	"""Locust's own method (calculate_response_time_percentile), so the values per request
	name match its CSV files and HTML report."""
	threshold = int(count * share)
	processed = 0
	for value in sorted(times, reverse=True):
		processed += times[value]
		if count - processed <= threshold:
			return value
	return 0


class Aggregate:
	def __init__(self) -> None:
		self.times: Counter = Counter()
		self.requests = 0
		self.failures = 0
		self.total = 0.0
		self.max = 0.0

	def add(self, entry: dict) -> None:
		self.times.update({int(float(k)): v for k, v in (entry.get("response_times") or {}).items()})
		self.requests += entry.get("num_requests") or 0
		self.failures += entry.get("num_failures") or 0
		self.total += entry.get("total_response_time") or 0.0
		self.max = max(self.max, entry.get("max_response_time") or 0.0)

	def row(self) -> dict:
		count = sum(self.times.values())
		out = {
			"requests": self.requests,
			"failures": self.failures,
			"fail_ratio": self.failures / self.requests if self.requests else 0.0,
			"mean_ms": round(self.total / self.requests) if self.requests else 0,
			"max_ms": round(self.max),
		}
		for share in PERCENTILES:
			out[f"p{share * 100:g}_ms"] = percentile(self.times, count, share) if count else 0
		return out


def group_of(name: str) -> str:
	prefix, sep, _rest = name.partition(":")
	return prefix if sep and prefix in ("read", "write", "job") else "other"


def operation_of(name: str) -> str:
	prefix, sep, rest = name.partition(":")
	return f"{prefix}:{rest.split(' ', 1)[0]}" if sep else name


def aggregate(entries: list[dict], key) -> dict[str, dict]:
	groups: dict[str, Aggregate] = {}
	for entry in entries:
		groups.setdefault(key(entry["name"]), Aggregate()).add(entry)
	return {name: agg.row() for name, agg in sorted(groups.items())}


def exceptions_in(results: Path) -> int:
	path = results / "locust_exceptions.csv"
	if not path.exists():
		return 0
	with path.open(newline="") as handle:
		return sum(int(row.get("Count") or 0) for row in csv.DictReader(handle))


# ---------------------------------------------------------------------------------------
# Server side (metrics endpoint)
# ---------------------------------------------------------------------------------------


def parse_metrics(text: str) -> list[tuple[str, dict, float]]:
	samples = []
	for line in text.splitlines():
		match = SAMPLE.match(line)
		if not match or line.startswith("#"):
			continue
		labels = dict(LABEL.findall(match["labels"] or ""))
		samples.append((match["name"], labels, float(match["value"])))
	return samples


def bound(le: str) -> float:
	return math.inf if le == "+Inf" else float(le)


def histogram_delta(before: list, after: list) -> dict[tuple[str, str], dict]:
	"""Requests the server timed during the run, per (kind, verb): bucket counts of the
	second snapshot minus the first."""

	def buckets(samples):
		out: dict[tuple[str, str], dict[str, float]] = {}
		for name, labels, value in samples:
			if name == f"{HISTOGRAM}_bucket":
				out.setdefault((labels.get("kind", ""), labels.get("verb", "")), {})[labels["le"]] = value
		return out

	first, second = buckets(before), buckets(after)
	delta = {}
	for key, counts in second.items():
		previous = first.get(key, {})
		cumulative = sorted(((bound(le), value - previous.get(le, 0.0)) for le, value in counts.items()))
		total = cumulative[-1][1] if cumulative else 0.0
		if total > 0:
			delta[key] = {"buckets": cumulative, "count": total}
	return delta


def histogram_quantile(buckets: list[tuple[float, float]], share: float) -> float:
	"""Prometheus' histogram_quantile: linear interpolation inside the bucket that holds the
	rank; in the +Inf bucket, the highest finite bound."""
	total = buckets[-1][1]
	rank = share * total
	lower, below = 0.0, 0.0
	for upper, count in buckets:
		if count >= rank:
			if math.isinf(upper):
				return lower
			return lower + (upper - lower) * ((rank - below) / (count - below) if count > below else 1.0)
		lower, below = upper, count
	return lower


def share_within(buckets: list[tuple[float, float]], limit: float) -> float | None:
	exact = [count for upper, count in buckets if upper == limit]
	return exact[0] / buckets[-1][1] if exact else None


def failed_jobs(samples: list) -> float:
	return sum(value for name, _labels, value in samples if name == "erp_queue_failed_jobs")


def server_side(results: Path, targets: dict[str, float]) -> dict | None:
	before, after = results / "metrics-before.prom", results / "metrics-after.prom"
	if not (before.exists() and after.exists()):
		return None
	first, second = parse_metrics(before.read_text()), parse_metrics(after.read_text())
	rows = {}
	for (kind, verb), data in sorted(histogram_delta(first, second).items()):
		target = targets.get(verb)
		rows[f"{kind}/{verb}"] = {
			"requests": int(data["count"]),
			**{
				f"p{share * 100:g}_ms": round(histogram_quantile(data["buckets"], share) * 1000)
				for share in PERCENTILES
			},
			"share_within_target": share_within(data["buckets"], target) if target else None,
			"target_s": target,
		}
	return {
		"histogram": rows,
		"failed_jobs_before": failed_jobs(first),
		"failed_jobs_after": failed_jobs(second),
	}


def scrape(url: str, token_file: str, output: str) -> int:
	import requests

	token = Path(token_file).read_text().strip()
	response = requests.get(
		f"{url.rstrip('/')}/api/method/automotive_core.ops.health.metrics",
		headers={"X-Hinode-Metrics-Token": token},
		timeout=60,
	)
	if response.status_code != 200 or HISTOGRAM not in response.text:
		print(
			f"metrics endpoint answered HTTP {response.status_code} without the request histogram "
			"(wrong token, or no automotive_metrics_token on the site)",
			file=sys.stderr,
		)
		return 1
	Path(output).write_text(response.text)
	return 0


def sample(url: str, token_file: str, output: str, every: str = "15") -> int:
	"""Until stopped: the queue lengths every few seconds, a line per queue (time, queue, jobs)."""
	import time

	import requests

	token = Path(token_file).read_text().strip()
	with open(output, "a") as out:
		while True:
			try:
				response = requests.get(
					f"{url.rstrip('/')}/api/method/automotive_core.ops.health.metrics",
					headers={"X-Hinode-Metrics-Token": token},
					timeout=30,
				)
			except requests.RequestException:
				response = None
			now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
			for name, labels, value in parse_metrics(response.text if response is not None else ""):
				if name == "erp_queue_jobs" and labels.get("queue"):
					out.write(f"{now} {labels['queue']} {value:g}\n")
			out.flush()
			time.sleep(float(every))


SLOW_LITERALS = (
	(re.compile(r"'(?:[^'\\]|\\.)*'"), "?"),
	(re.compile(r"\b\d+(?:\.\d+)?\b"), "?"),
	(re.compile(r"\((?:\s*\?\s*,)+\s*\?\s*\)"), "(?, ...)"),
	(re.compile(r"\s+"), " "),
)


def statement_shape(sql: str) -> str:
	for pattern, replacement in SLOW_LITERALS:
		sql = pattern.sub(replacement, sql)
	return sql.strip().rstrip(";")[:400]


def slowlog(log_path: str, output: str) -> int:
	"""MariaDB's slow query log as statements grouped by shape: count, total and longest
	time, rows examined. Literal values are masked, so the digest carries no data."""
	groups: dict[str, dict] = {}
	entry: dict | None = None
	lines: list[str] = []

	def close() -> None:
		if entry is None:
			return
		sql = " ".join(ln for ln in lines if not ln.lower().startswith(("set timestamp", "use ")))
		if not sql.strip():
			return
		row = groups.setdefault(
			statement_shape(sql), {"count": 0, "total_s": 0.0, "max_s": 0.0, "rows_examined": 0}
		)
		row["count"] += 1
		row["total_s"] += entry["time"]
		row["max_s"] = max(row["max_s"], entry["time"])
		row["rows_examined"] += entry["examined"]

	timing = re.compile(r"# Query_time: ([\d.]+)\s+Lock_time: [\d.]+\s+Rows_sent: \d+\s+Rows_examined: (\d+)")
	for line in Path(log_path).read_text(errors="replace").splitlines():
		match = timing.match(line)
		if match:
			close()
			entry, lines = {"time": float(match[1]), "examined": int(match[2])}, []
		elif line.startswith("#") or entry is None:
			continue
		else:
			lines.append(line.strip())
	close()
	rows = [
		{"statement": shape, **row, "total_s": round(row["total_s"], 3), "max_s": round(row["max_s"], 3)}
		for shape, row in sorted(groups.items(), key=lambda item: -item[1]["total_s"])
	]
	Path(output).write_text(json.dumps({"statements": rows}, indent=1) + "\n")
	return 0


def throughput(entries: list[dict]) -> dict | None:
	"""Requests per second over the phase, from the first request's start to the last request,
	and the busiest second."""
	starts = [e["start_time"] for e in entries if e.get("start_time")]
	ends = [e["last_request_timestamp"] for e in entries if e.get("last_request_timestamp")]
	if not starts or not ends or max(ends) <= min(starts):
		return None
	seconds = max(ends) - min(starts)
	by_group: Counter = Counter()
	per_second: Counter = Counter()
	for e in entries:
		by_group[group_of(e["name"])] += e.get("num_requests") or 0
		per_second.update({k: v for k, v in (e.get("num_reqs_per_sec") or {}).items()})
	total = sum(by_group.values())
	return {
		"seconds": round(seconds, 1),
		"requests": total,
		"per_second": round(total / seconds, 2),
		"busiest_second": max(per_second.values()) if per_second else None,
		"per_second_by_group": {g: round(n / seconds, 2) for g, n in sorted(by_group.items())},
	}


def queue_samples(results: Path) -> dict | None:
	path = results / "queues.txt"
	if not path.exists():
		return None
	series: dict[str, list[float]] = {}
	for line in path.read_text().splitlines():
		parts = line.split()
		if len(parts) == 3:
			series.setdefault(parts[1], []).append(float(parts[2]))
	return {
		queue: {"samples": len(values), "max": max(values), "mean": round(sum(values) / len(values), 1)}
		for queue, values in sorted(series.items())
	} or None


def storage(results: Path) -> dict | None:
	"""The site database (InnoDB's estimated rows; data and index bytes) and its files, before
	and after the phase."""
	before, after = results / "db-before.json", results / "db-after.json"
	if not (before.exists() and after.exists()):
		return None
	first, second = json.loads(before.read_text()), json.loads(after.read_text())
	tables = {
		name: {
			"bytes_before": first.get(name, {}).get("bytes", 0),
			"bytes_after": row["bytes"],
			"rows_after": row["rows"],
			"rows_growth": row["rows"] - first.get(name, {}).get("rows", 0),
		}
		for name, row in second.items()
	}
	out = {
		"database_bytes_before": sum(t["bytes"] for t in first.values()),
		"database_bytes_after": sum(t["bytes"] for t in second.values()),
		"largest_tables": dict(sorted(tables.items(), key=lambda item: -item[1]["bytes_after"])[:10]),
		"most_rows_added": dict(sorted(tables.items(), key=lambda item: -item[1]["rows_growth"])[:10]),
	}
	for label in ("before", "after"):
		path = results / f"files-{label}.txt"
		if path.exists():
			out[f"files_bytes_{label}"] = sum(
				int(line.split()[0]) for line in path.read_text().splitlines() if line.split()[:1] != []
			)
	return out


# ---------------------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------------------


def table(title: str, rows: dict[str, dict], extra: list[tuple[str, str]] | None = None) -> list[str]:
	columns = [
		("requests", "requests"),
		("fail %", "fail_ratio"),
		("p50", "p50_ms"),
		("p95", "p95_ms"),
		("p99", "p99_ms"),
		("max", "max_ms"),
		*(extra or []),
	]
	width = max([len(name) for name in rows] + [len(title.split(" (")[0]), 12])
	lines = [title, f"{'':<{width}}  " + "  ".join(f"{label:>9}" for label, _ in columns)]
	for name, row in rows.items():
		cells = []
		for _label, key in columns:
			value = row.get(key)
			if key == "fail_ratio":
				cells.append(f"{value * 100:9.2f}")
			elif isinstance(value, float) and key != "fail_ratio":
				cells.append(f"{value:9.3f}")
			else:
				cells.append(f"{'-' if value is None else value:>9}")
		lines.append(f"{name:<{width}}  " + "  ".join(cells))
	return [*lines, ""]


def evaluate(results: Path, targets_s: dict[str, float], max_fail_ratio: float) -> int:
	stats_file = results / "stats.json"
	if not stats_file.exists():
		print(f"no {stats_file}: the run did not finish (see locust.log)", file=sys.stderr)
		return 2
	entries = [
		e for e in json.loads(stats_file.read_text()) if e.get("num_requests") or e.get("num_failures")
	]
	if not entries:
		print("no requests were made", file=sys.stderr)
		return 2
	run = json.loads((results / "run.json").read_text()) if (results / "run.json").exists() else {}
	groups = aggregate(entries, group_of)
	operations = aggregate(entries, operation_of)
	requests = aggregate(entries, lambda name: name)
	exceptions = exceptions_in(results)
	total = sum(g["requests"] for g in groups.values())
	failures = sum(g["failures"] for g in groups.values())

	reasons = []
	for group, target in targets_s.items():
		row = groups.get(group)
		if row is None:
			reasons.append(f"no {group} requests: the run cannot show the {group} target")
			continue
		row["target_p95_ms"] = round(target * 1000)
		row["result"] = "ok" if row["p95_ms"] <= target * 1000 else "MISSED"
		if row["result"] != "ok":
			reasons.append(f"{group} p95 {row['p95_ms']} ms > {target:g} s")
	if total and failures / total > max_fail_ratio:
		reasons.append(
			f"{failures} of {total} requests failed ({failures / total:.2%} > {max_fail_ratio:.2%})"
		)
	if exceptions:
		reasons.append(f"{exceptions} errors inside the test itself (locust_exceptions.csv)")
	over = {
		name: row
		for name, row in requests.items()
		if group_of(name) in targets_s and row["p95_ms"] > targets_s[group_of(name)] * 1000
	}
	slowest = dict(sorted(requests.items(), key=lambda item: -item[1]["p95_ms"])[:15])
	server = server_side(results, {"read": targets_s.get("read"), "write": targets_s.get("write")})
	rate = throughput(entries)
	queues = queue_samples(results)
	disk = storage(results)
	slow_path = results / "slow-queries.json"
	slow = json.loads(slow_path.read_text())["statements"] if slow_path.exists() else None

	lines = []
	if run:
		lines += [
			f"Run {results.name}: {run.get('users')} users (+{run.get('spawn_rate')}/s) for {run.get('duration')} "
			f"against {run.get('url')}; commit {run.get('git_commit')}{' (dirty)' if run.get('git_dirty') else ''}"
			+ (f"; {run['label']}" if run.get("label") else ""),
			"",
		]
	reads = groups.get("read", {}).get("requests", 0)
	writes = groups.get("write", {}).get("requests", 0)
	lines += table(
		"Request groups (client-side response time, ms)",
		groups,
		[("target", "target_p95_ms"), ("result", "result")],
	)
	if reads + writes:
		lines += [
			f"Mix: {reads} reads / {writes} writes ({reads / (reads + writes):.0%} / {writes / (reads + writes):.0%})",
			"",
		]
	if rate:
		groups_rate = ", ".join(f"{g} {r:g}/s" for g, r in rate["per_second_by_group"].items())
		lines += [
			f"Throughput: {rate['requests']} requests in {rate['seconds']:g} s, {rate['per_second']:g} per second "
			f"({groups_rate}); busiest second {rate['busiest_second']}",
			"",
		]
	lines += table("Operations", operations)
	lines += table("Slowest requests by p95", slowest)
	if over:
		lines += table("Requests over their group's p95 target", over)
	if server:
		lines += table(
			"Server side: erp_http_request_duration_seconds over the run (ms, bucket estimates)",
			server["histogram"],
			[("in target", "share_within_target")],
		)
		lines += [
			f"Failed background jobs (erp_queue_failed_jobs): {server['failed_jobs_before']:g} before, "
			f"{server['failed_jobs_after']:g} after",
			"",
		]
	if queues:
		lines += ["Queue lengths during the phase (sampled every 15 s):"]
		lines += [
			f"  {q}: max {row['max']:g}, mean {row['mean']:g} ({row['samples']} samples)"
			for q, row in queues.items()
		]
		lines.append("")
	if slow is not None:
		lines += [
			f"Slow statements (over the slow query log's threshold): {sum(r['count'] for r in slow)} in {len(slow)} shapes"
		]
		lines += [
			f"  {r['count']} x, {r['total_s']:g} s total, {r['max_s']:g} s max, {r['rows_examined']} rows examined: {r['statement'][:160]}"
			for r in slow[:10]
		]
		lines.append("")
	if disk:
		mib = 1024 * 1024
		lines += [
			f"Database: {disk['database_bytes_before'] / mib:.1f} MiB before, {disk['database_bytes_after'] / mib:.1f} MiB after"
			+ (
				f"; files {disk['files_bytes_before'] / mib:.1f} MiB before, {disk['files_bytes_after'] / mib:.1f} MiB after"
				if "files_bytes_before" in disk and "files_bytes_after" in disk
				else ""
			),
			"  most rows added: "
			+ ", ".join(
				f"{name} +{row['rows_growth']}"
				for name, row in disk["most_rows_added"].items()
				if row["rows_growth"] > 0
			),
			"",
		]
	lines.append("RESULT: " + ("targets met" if not reasons else "FAILED - " + "; ".join(reasons)))
	text = "\n".join(lines)
	print(text)
	(results / "summary.txt").write_text(text + "\n")
	(results / "summary.json").write_text(
		json.dumps(
			{
				"run": run,
				"targets_p95_s": targets_s,
				"max_fail_ratio": max_fail_ratio,
				"passed": not reasons,
				"reasons": reasons,
				"exceptions": exceptions,
				"groups": groups,
				"operations": operations,
				"requests": requests,
				"server": server,
				"throughput": rate,
				"queues": queues,
				"slow_statements": slow,
				"storage": disk,
			},
			indent=1,
			sort_keys=True,
		)
		+ "\n"
	)
	return 0 if not reasons else 1


def main(argv: list[str]) -> int:
	if argv[:1] == ["scrape"]:
		if len(argv) != 4:
			print("usage: summary.py scrape URL TOKEN_FILE OUTPUT", file=sys.stderr)
			return 2
		return scrape(*argv[1:])
	if argv[:1] == ["sample"]:
		if len(argv) not in (4, 5):
			print("usage: summary.py sample URL TOKEN_FILE OUTPUT [SECONDS]", file=sys.stderr)
			return 2
		return sample(*argv[1:])
	if argv[:1] == ["slowlog"]:
		if len(argv) != 3:
			print("usage: summary.py slowlog SLOW_LOG OUTPUT_JSON", file=sys.stderr)
			return 2
		return slowlog(*argv[1:])
	parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	parser.add_argument("results", type=Path)
	parser.add_argument("--read-p95", type=float, default=2.0, help="seconds (default 2)")
	parser.add_argument("--write-p95", type=float, default=4.0, help="seconds (default 4)")
	parser.add_argument(
		"--max-fail-ratio", type=float, default=0.01, help="share of failed requests (default 0.01)"
	)
	args = parser.parse_args(argv)
	return evaluate(args.results, {"read": args.read_p95, "write": args.write_p95}, args.max_fail_ratio)


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
