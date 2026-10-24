#!/usr/bin/env python3
"""Check translation catalogues: they parse, every message is translated (with
--require-complete), and each translation keeps the placeholders and HTML tags of its
source (a lost ``{0}`` or ``%s`` breaks the message at run time).

	python3 scripts/dev/check_po.py [--require-complete] apps/*/*/locale/ja.po
"""

from __future__ import annotations

import re
import sys
from collections import Counter

from babel.messages.pofile import read_po

PLACEHOLDER = re.compile(r"\{\d*\}|\{[a-z_]+\}|%\([a-z_]+\)[sd]|%[sd]")
TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*\b")


def check(path: str, require_complete: bool) -> list[str]:
	problems = []
	with open(path, "rb") as handle:
		catalog = read_po(handle)
	total = untranslated = 0
	for message in catalog:
		if not message.id:
			continue
		total += 1
		source = message.id if isinstance(message.id, str) else message.id[0]
		target = message.string if isinstance(message.string, str) else (message.string or [""])[0]
		if not target:
			untranslated += 1
			continue
		where = f"{path}: {source[:60]!r}"
		if Counter(PLACEHOLDER.findall(source)) != Counter(PLACEHOLDER.findall(target)):
			problems.append(f"{where}: placeholders {PLACEHOLDER.findall(source)} -> {PLACEHOLDER.findall(target)}")
		if Counter(t.lower() for t in TAG.findall(source)) != Counter(t.lower() for t in TAG.findall(target)):
			problems.append(f"{where}: HTML tags differ")
	if require_complete and untranslated:
		problems.append(f"{path}: {untranslated} of {total} messages untranslated")
	print(f"{path}: {total - untranslated}/{total} translated")
	return problems


def main(argv: list[str]) -> int:
	require_complete = "--require-complete" in argv
	problems = []
	for path in [a for a in argv if not a.startswith("--")]:
		problems += check(path, require_complete)
	for problem in problems:
		print(problem, file=sys.stderr)
	return 1 if problems else 0


if __name__ == "__main__":
	sys.exit(main(sys.argv[1:]))
