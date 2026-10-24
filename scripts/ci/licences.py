#!/usr/bin/env python3
"""Licence inventory of an application image from its SPDX SBOM (GATE-9, ARC-2).

	python3 scripts/ci/licences.py .local/sbom/<image>.spdx.json > inventory.md

Every package's declared licence (the concluded one when nothing is declared), grouped by
ecosystem (Debian, Python, Node, other) and by class: permissive, weak copyleft (file or
library level), strong copyleft, network copyleft (AGPL: obligations towards users of a
network service) and unknown (no licence recorded, or one the classifier does not know). The
non-permissive and the unknown packages are listed one by one for the owner's review; the
permissive ones are counted by licence. ``LicenseRef-`` names (Debian copyright files) are
classified by the licence family their name gives; hashed ones (texts syft could not
identify) stay unknown. sbom.sh writes it beside each SBOM; the committed copy
is ops/security/licences.md. Exit status 0: the inventory is information, the decisions are
the owner's (DECISIONS.md D-025).
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict

ECOSYSTEMS = {"deb": "Debian packages", "pypi": "Python packages", "npm": "Node packages"}
CLASSES = ("network copyleft", "strong copyleft", "weak copyleft", "unknown", "permissive")
PERMISSIVE = re.compile(r"^(MIT|MIT-0|X11|ISC|0BSD|BSD-[0-9]-Clause.*|BSD-Source-Code|Apache-[0-9.]+|Zlib|PSF-2\.0|"
	r"Python-2\.0(\.1)?|Unicode-.*|CC0-1\.0|CC-BY-[0-9.]+|Unlicense|WTFPL|BSL-1\.0|Beerware|curl|NTP|OpenSSL|SSLeay|"
	r"HPND.*|Libpng|libpng-2\.0|IJG|Info-ZIP|FSFAP|FSFUL|FSFULLR|MIT-.*|bzip2-1\.0\.6|TCL|Spencer-.*|ICU|"
	r"BlueOak-1\.0\.0|Artistic-2\.0|Artistic-1\.0.*|PostgreSQL|W3C.*|Python-2\.0\.1|AFL-[0-9.]+|Ruby|OFL-1\.1|ZPL-[0-9.]+|libtiff)$")
WEAK = re.compile(r"^(LGPL-.*|MPL-.*|EPL-.*|CDDL-.*|GFDL-.*|LGPLLR|CPL-1\.0|Classpath-exception-.*)$")
STRONG = re.compile(r"^(GPL-.*|GPL-1\.0\+?|GPL-2\.0\+?|GPL-3\.0\+?)$")
NETWORK = re.compile(r"^(AGPL-.*|SSPL-.*|EUPL-.*)$")
# LicenseRef names by family: the first pattern that matches gives the class's representative
FAMILIES = [(re.compile(p), licence) for p, licence in (
	(r"agpl|affero", "AGPL-3.0-or-later"),
	(r"lgpl|lesser|gnulib|library-general", "LGPL-2.0-or-later"),
	(r"gpl|gnu-general", "GPL-1.0-or-later"),
	(r"mpl|mozilla", "MPL-2.0"),
	(r"bsd|mit|expat|x11|apache|isc|zlib|public-?domain|psf|python|bzip|libpng|ijg|ntp|rfc|unicode|chromium|"
		r"llvm|boost|curl|openssl|ssleay|hpnd|artistic|permissive|unlicense|cc0|beerware|tcp-wrappers|"
		r"google|sqlite|libtiff|glarson|fsf", "MIT"),
)]
HASHED = re.compile(r"^[0-9a-f]{40,}$")
# packages whose metadata in the image names no licence, curated from their licence files:
# the custom apps (pyproject.toml, LICENSE), the upstream framework and the runtimes
CURATED = {
	"automotive-core": "GPL-3.0-only", "automotive-manufacturing": "GPL-3.0-only",
	"automotive-portals": "GPL-3.0-only", "automotive-supply-chain": "GPL-3.0-only",
	"frappe": "MIT", "erpnext": "GPL-3.0-only", "banking": "GPL-3.0-only", "python": "PSF-2.0", "node": "MIT",
	"github.com/restic/restic/cmd/restic": "BSD-2-Clause", "wkhtmltox": "LGPL-3.0-or-later",
	"libintl": "LGPL-2.1-or-later", "jinja2": "BSD-3-Clause", "marshmallow": "MIT", "sqlparse": "BSD-3-Clause",
	"prompt-toolkit": "BSD-3-Clause", "python-dateutil": "Apache-2.0 OR BSD-3-Clause", "duckdb": "MIT",
	"pdfplumber": "MIT", "tinycss2": "BSD-3-Clause", "cssselect2": "BSD-3-Clause", "pydyf": "BSD-3-Clause",
	"tinyhtml5": "MIT", "pyphen": "GPL-2.0-or-later OR LGPL-2.1-or-later OR MPL-1.1", "ptyprocess": "ISC",
	"traceback-with-variables": "MIT", "engine.io-parser": "MIT", "wcwidth": "MIT", "weasyprint": "BSD-3-Clause",
	"webencodings": "BSD-3-Clause", "toyota-erp/erp": "GPL-3.0-only",
	# setuptools' Windows launchers (Simple Launcher, cli*.exe, gui*.exe) ship inside setuptools
	"Simple Launcher": "MIT", "cli": "MIT", "cli-32": "MIT", "cli-64": "MIT", "cli-arm64": "MIT",
	"gui": "MIT", "gui-32": "MIT", "gui-64": "MIT", "gui-arm64": "MIT",
}
UNNAMED = {"NOASSERTION", "NONE", "LicenseRef-UNKNOWN", "LicenseRef-Dual-License"}


def ecosystem(package: dict) -> str:
	purl = next((ref["referenceLocator"] for ref in package.get("externalRefs", [])
		if ref.get("referenceType") == "purl"), "")
	kind = purl[4:].split("/", 1)[0] if purl.startswith("pkg:") else "other"
	return kind if kind in ECOSYSTEMS else "other"


def terms(expression: str) -> list[str]:
	"""The licence identifiers of an SPDX expression, aliases resolved."""
	out = []
	for term in re.split(r"\s+(?:AND|OR|WITH)\s+|[()]", expression or ""):
		term = term.strip()
		if not term:
			continue
		if term.startswith("LicenseRef-"):
			key = term[len("LicenseRef-"):].lower()
			if not HASHED.match(key):
				term = next((licence for pattern, licence in FAMILIES if pattern.search(key)), term)
		out.append(term)
	return out


def classify(expression: str) -> str:
	"""The most demanding class among the expression's licences (a package under "A AND B"
	carries both; under "A OR B" the receiver may choose, but both are listed for review)."""
	found = terms(expression)
	if not found or expression in ("NOASSERTION", "NONE"):
		return "unknown"
	classes = set()
	for term in found:
		if NETWORK.match(term):
			classes.add("network copyleft")
		elif STRONG.match(term):
			classes.add("strong copyleft")
		elif WEAK.match(term):
			classes.add("weak copyleft")
		elif PERMISSIVE.match(term):
			classes.add("permissive")
		else:
			classes.add("unknown")
	return next(c for c in CLASSES if c in classes)


def inventory(sbom: dict) -> str:
	rows = defaultdict(list)
	for package in sbom.get("packages", []):
		licence = package.get("licenseDeclared")
		if not licence or licence in UNNAMED:
			licence = package.get("licenseConcluded") or "NOASSERTION"
		if licence in UNNAMED and package.get("name") in CURATED:
			licence = f"{CURATED[package['name']]} (curated)"
		rows[(ecosystem(package), classify(licence.removesuffix(' (curated)')))].append((package.get("name", "?"),
			package.get("versionInfo", ""), licence))
	name = sbom.get("name", "the image")
	version = next((p.get("versionInfo") for p in sbom.get("packages", []) if p.get("name") == name), "")
	created = (sbom.get("creationInfo") or {}).get("created", "")
	total = sum(len(v) for v in rows.values())
	lines = [f"# Licence inventory: {name} {version}".rstrip(), "",
		f"{total} packages from the image's SPDX SBOM of {created or 'an unknown date'} (`scripts/ci/sbom.sh`), "
		"classified by "
		"`scripts/ci/licences.py`. Classes: permissive; weak copyleft (LGPL, MPL, EPL: obligations on "
		"the component itself); strong copyleft (GPL: on works distributed with it); network copyleft "
		"(AGPL and similar: also towards users of a network service); unknown (no licence recorded, "
		"or one not classified). The owner's licence decisions are DECISIONS.md D-025.", "",
		"| Ecosystem | " + " | ".join(c.capitalize() for c in CLASSES) + " | Total |",
		"|---|" + "---|" * (len(CLASSES) + 1)]
	for key in (*ECOSYSTEMS, "other"):
		counts = [len(rows[(key, c)]) for c in CLASSES]
		if sum(counts):
			lines.append(f"| {ECOSYSTEMS.get(key, 'Other')} | " + " | ".join(map(str, counts)) + f" | {sum(counts)} |")
	for cls in CLASSES[:-1]:
		listed = sorted((eco, n, v, lic) for (eco, c), items in rows.items() if c == cls for n, v, lic in items)
		lines += ["", f"## {cls.capitalize()} ({len(listed)})", ""]
		if not listed:
			lines.append("None.")
			continue
		lines += ["| Ecosystem | Package | Version | Licence |", "|---|---|---|---|"]
		lines += [f"| {eco} | {n} | {v} | {lic.replace('|', '/')} |" for eco, n, v, lic in listed]
	permissive = Counter(lic for (eco, c), items in rows.items() if c == "permissive" for _, _, lic in items)
	lines += ["", f"## Permissive ({sum(permissive.values())}), by licence", "", "| Licence | Packages |", "|---|---|"]
	lines += [f"| {lic.replace('|', '/')} | {count} |" for lic, count in sorted(permissive.items(), key=lambda i: (-i[1], i[0]))]
	return "\n".join(lines) + "\n"


def main() -> int:
	with open(sys.argv[1]) as handle:
		sys.stdout.write(inventory(json.load(handle)))
	return 0


if __name__ == "__main__":
	sys.exit(main())
