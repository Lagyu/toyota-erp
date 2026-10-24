#!/usr/bin/env python3
"""Markdown summary of a dataset segment for the CI job summary.

	dataset_summary.py STATUS_JSON [SEGMENT]

STATUS_JSON is scripts/ci/dataset.sh's status.json (automotive_seed.runner.status): the
run's status and simulated date, the generated document counts, and the validation checks
once the run has reached them.
"""

from __future__ import annotations

import json
import sys


def main() -> int:
	status = json.load(open(sys.argv[1]))
	segment = f"Segment {sys.argv[2]}: " if len(sys.argv) > 2 else ""
	print(f"### {segment}{status.get('run')} {status.get('status')}")
	print()
	print(
		f"Simulated to **{status.get('simulated_date') or '-'}** of {status.get('start_date')} .. "
		f"{status.get('end_date')} (stage `{status.get('stage')}`)."
	)
	if status.get("error"):
		print("\n```\n" + str(status["error"])[-3000:] + "\n```")
	counts = status.get("counts") or {}
	if counts:
		print("\n<details><summary>Generated documents</summary>\n")
		print("| Document | Count |\n|---|---:|")
		for doctype, count in sorted(counts.items()):
			print(f"| {doctype} | {count:,} |")
		print("\n</details>")
	validation = status.get("validation")
	if validation:
		checks = validation.get("checks", [])
		failed = [c for c in checks if not c.get("ok")]
		print(
			f"\n**Validation: {'passed' if validation.get('ok') else 'FAILED'}** "
			f"({len(checks) - len(failed)}/{len(checks)} checks)\n"
		)
		print("| Check | Result | Detail |\n|---|---|---|")
		for check in checks:
			detail = json.dumps(check.get("detail"), default=str)
			detail = detail if len(detail) <= 160 else detail[:157] + "..."
			print(f"| {check.get('check')} | {'ok' if check.get('ok') else '**FAILED**'} | `{detail}` |")
	print()
	return 0


if __name__ == "__main__":
	sys.exit(main())
