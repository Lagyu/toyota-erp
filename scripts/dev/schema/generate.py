#!/usr/bin/env python3
"""Render the DocType specs in apps/<app>/schema/*.py into Frappe DocType JSON.

    generate.py            write changed DocType JSON (+ missing package stubs)
    generate.py --check    exit 1 if any generated file is missing or stale (CI)
    generate.py --app X    restrict to one app

Each spec module exposes ``DOCTYPES: list[dsl.DocType]``. Output goes to
``apps/<app>/<app>/<module>/doctype/<doctype>/<doctype>.json``; ``creation`` and
``modified`` are kept when the rendered content is unchanged, so re-running is a
no-op. Controller stubs are created once and never overwritten.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dsl import DocType, class_name, scrub  # noqa: E402

REPO = Path(__file__).resolve().parents[3]
APPS_DIR = REPO / "apps"

CONTROLLER_STUB = '''# Copyright (c) 2026, Toyota ERP Clone contributors
# License: GNU General Public License v3. See license.txt

from frappe.model.document import Document


class {cls}(Document):
	pass
'''


def load_specs(app_dir: Path) -> list[DocType]:
	specs: list[DocType] = []
	for path in sorted((app_dir / "schema").glob("*.py")):
		if path.name.startswith("_"):
			continue
		module_spec = importlib.util.spec_from_file_location(f"schema_{app_dir.name}_{path.stem}", path)
		module = importlib.util.module_from_spec(module_spec)
		module_spec.loader.exec_module(module)
		specs.extend(getattr(module, "DOCTYPES", []))
	return specs


def dumps(doc: dict) -> str:
	# Same layout as frappe.as_json(doc, indent=1): sorted keys, one-space indent.
	return json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def render(spec: DocType, existing: dict | None) -> dict:
	doc = spec.to_dict()
	now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
	doc["creation"] = (existing or {}).get("creation", now)
	if existing:
		previous = {k: v for k, v in existing.items() if k != "modified"}
		if previous == doc:
			doc["modified"] = existing.get("modified", now)
			return doc
	doc["modified"] = now
	return doc


def ensure_file(path: Path, content: str, check: bool, problems: list[str]) -> None:
	if path.exists():
		return
	if check:
		problems.append(f"missing {path.relative_to(REPO)}")
		return
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)


def process_app(app_dir: Path, check: bool, problems: list[str]) -> int:
	app = app_dir.name
	package = app_dir / app
	modules_txt = package / "modules.txt"
	declared_modules = set(modules_txt.read_text().split("\n")) if modules_txt.exists() else set()
	specs = load_specs(app_dir)
	seen: set[str] = set()
	written = 0
	for spec in specs:
		if spec.name in seen:
			problems.append(f"{app}: DocType {spec.name} defined twice")
			continue
		seen.add(spec.name)
		if spec.module not in declared_modules:
			problems.append(f"{app}: module {spec.module!r} of {spec.name} missing from modules.txt")
		module_dir = package / scrub(spec.module)
		doctype_dir = module_dir / "doctype" / spec.scrubbed
		json_path = doctype_dir / f"{spec.scrubbed}.json"
		for init in (module_dir / "__init__.py", module_dir / "doctype" / "__init__.py", doctype_dir / "__init__.py"):
			ensure_file(init, "", check, problems)
		ensure_file(
			doctype_dir / f"{spec.scrubbed}.py", CONTROLLER_STUB.format(cls=class_name(spec.name)), check, problems
		)
		existing = json.loads(json_path.read_text()) if json_path.exists() else None
		try:
			rendered = render(spec, existing)
		except ValueError as exc:
			problems.append(f"{app}: {exc}")
			continue
		if existing == rendered:
			continue
		if check:
			problems.append(f"stale {json_path.relative_to(REPO)}")
			continue
		json_path.parent.mkdir(parents=True, exist_ok=True)
		json_path.write_text(dumps(rendered))
		written += 1
	return written


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument("--check", action="store_true", help="fail if generated files are missing or stale")
	parser.add_argument("--app", help="only this app")
	args = parser.parse_args()

	problems: list[str] = []
	total = 0
	for app_dir in sorted(p for p in APPS_DIR.iterdir() if (p / "schema").is_dir()):
		if args.app and app_dir.name != args.app:
			continue
		total += process_app(app_dir, args.check, problems)
	for problem in problems:
		print(problem, file=sys.stderr)
	if problems:
		return 1
	if not args.check:
		print(f"schema: {total} DocType file(s) written")
	return 0


if __name__ == "__main__":
	sys.exit(main())
