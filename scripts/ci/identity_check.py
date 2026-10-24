#!/usr/bin/env python3
"""Identity boundary (requirements §1): customer-facing assets — templates, web pages,
portal scripts and styles, print formats, translations, fixtures and seed data — must
not carry the trademark of the company this clone is modelled on, its logos or its
internal names. The working name "Toyota ERP Clone" may appear only in developer-facing
metadata (README, docs, licence headers, app_publisher)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CUSTOMER_FACING = [
	"apps/*/*/www/**/*",
	"apps/*/*/templates/**/*",
	"apps/*/*/public/**/*",
	"apps/*/*/**/print_format/**/*",
	"apps/*/*/translations/**/*",
	"apps/*/*/locale/**/*",
	"apps/*/*/setup/tenants/**/*",
	"apps/automotive_seed/automotive_seed/generator/**/*",
	"apps/automotive_seed/automotive_seed/profiles/**/*",
	"apps/*/*/**/workspace/**/*.json",
	"apps/*/*/**/sidebar/**/*.json",
]
FORBIDDEN = re.compile(r"toyota|lexus|daihatsu|hino motors|denso|aisin|kanban\s*system\s*®|tps\s*®", re.I)
# licence headers name the working title as publisher (source files and gettext catalogues)
ALLOWED_LINES = re.compile(r"Copyright \([cC]\) 2026,? Toyota ERP Clone contributors")


def main() -> int:
	problems = []
	seen = set()
	for pattern in CUSTOMER_FACING:
		for path in ROOT.glob(pattern):
			if not path.is_file() or path in seen or "dist" in path.parts or path.suffix in {".pyc", ".map"}:
				continue
			seen.add(path)
			try:
				text = path.read_text(encoding="utf-8")
			except UnicodeDecodeError:
				continue
			for number, line in enumerate(text.splitlines(), 1):
				if FORBIDDEN.search(line) and not ALLOWED_LINES.search(line):
					problems.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:120]}")
	if problems:
		print("Customer-facing assets mention a protected name:")
		print("\n".join(problems))
		return 1
	print(f"ok ({len(seen)} files)")
	return 0


if __name__ == "__main__":
	sys.exit(main())
