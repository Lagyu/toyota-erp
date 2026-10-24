#!/usr/bin/env python3
"""Two dataset runs' fingerprints (status.json of dataset.yml exports: the validation's
fingerprint, apps/automotive_seed/automotive_seed/generator/validate.py) compared DocType by
DocType: the same profile and seed on the same code must give the same documents, whether a
run was interrupted or not.

	compare_datasets.py A/status.json B/status.json [--labels A B]

Prints a Markdown table; exit status 0 when every DocType's count and digest agree."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: str) -> tuple[dict, dict]:
	status = json.loads(Path(path).read_text())
	validation = status.get("validation") or {}
	if isinstance(validation, str):
		validation = json.loads(validation)
	return status, (validation.get("fingerprint") or {})


def main(argv: list[str]) -> int:
	parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
	parser.add_argument("a")
	parser.add_argument("b")
	parser.add_argument("--labels", nargs=2, default=("A", "B"))
	args = parser.parse_args(argv)
	(status_a, fp_a), (status_b, fp_b) = load(args.a), load(args.b)
	label_a, label_b = args.labels
	lines = [
		f"| | {label_a} | {label_b} |",
		"|---|---|---|",
		f"| run | {status_a.get('run')}: {status_a.get('status')} | {status_b.get('run')}: {status_b.get('status')} |",
		f"| simulated | {status_a.get('start_date')} .. {status_a.get('simulated_date')} "
		f"| {status_b.get('start_date')} .. {status_b.get('simulated_date')} |",
		"",
		"| DocType | Documents | Same documents |",
		"|---|---|---|",
	]
	if not fp_a or not fp_b:
		print("\n".join(lines))
		print("a run carries no fingerprint (validated before fingerprints were recorded?)")
		return 2
	different = []
	for doctype in sorted(set(fp_a["doctypes"]) | set(fp_b["doctypes"])):
		a, b = fp_a["doctypes"].get(doctype, {}), fp_b["doctypes"].get(doctype, {})
		same = a.get("sha256") == b.get("sha256") and a.get("count") == b.get("count")
		if not same:
			different.append(doctype)
		counts = (
			str(a.get("count"))
			if a.get("count") == b.get("count")
			else f"{a.get('count')} / {b.get('count')}"
		)
		lines.append(f"| {doctype} | {counts} | {'yes' if same else '**no**'} |")
	lines += [
		"",
		"Result: "
		+ (
			"the same documents (fingerprint " + fp_a["sha256"][:16] + ")"
			if not different
			else "different: " + ", ".join(different)
		),
	]
	print("\n".join(lines))
	return 0 if not different and fp_a["sha256"] == fp_b["sha256"] else 1


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
