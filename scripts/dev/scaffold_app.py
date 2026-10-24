#!/usr/bin/env python3
"""Create the skeleton of a Frappe v16 custom app under apps/ (non-interactive).

    scaffold_app.py APP_NAME "App Title" "Description" --requires app1,app2 --modules "Mod A,Mod B"

Never overwrites existing files, so it can also add new modules to an app.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PUBLISHER = "Toyota ERP Clone contributors"
EMAIL = "erp-team@hinode.example"

PYPROJECT = """[project]
name = "{app}"
authors = [
    {{ name = "{publisher}", email = "{email}" }}
]
description = "{description}"
requires-python = ">=3.14,<3.15"
readme = "README.md"
license = {{ text = "GPL-3.0-only" }}
dynamic = ["version"]
dependencies = [
    # frappe and erpnext are installed and pinned by bench (infra/versions.env).
]

[build-system]
requires = ["flit_core >=3.4,<4"]
build-backend = "flit_core.buildapi"

[tool.bench.frappe-dependencies]
frappe = ">=16.53.0,<17.0.0"
erpnext = ">=16.52.1,<17.0.0"

[tool.ruff]
extend = "../../ruff.toml"
"""

HOOKS = """app_name = "{app}"
app_title = "{title}"
app_publisher = "{publisher}"
app_description = "{description}"
app_email = "{email}"
app_license = "GPL-3.0-only"

required_apps = {requires!r}
"""

README = """# {title}

{description}

Part of Toyota ERP Clone; see the repository README and ARCHITECTURE.md.
Install order: {order}.
"""


def write(path: Path, content: str) -> None:
	if path.exists():
		return
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(content)
	print(f"created {path.relative_to(REPO)}")


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("app")
	parser.add_argument("title")
	parser.add_argument("description")
	parser.add_argument("--requires", default="")
	parser.add_argument("--modules", required=True)
	args = parser.parse_args()

	app = args.app
	requires = [r for r in args.requires.split(",") if r]
	modules = [m.strip() for m in args.modules.split(",") if m.strip()]
	root = REPO / "apps" / app
	pkg = root / app
	fmt = dict(
		app=app,
		title=args.title,
		description=args.description,
		publisher=PUBLISHER,
		email=EMAIL,
		requires=["frappe", "erpnext", *requires],
	)

	write(root / "pyproject.toml", PYPROJECT.format(**fmt))
	write(root / "README.md", README.format(**fmt, order=" → ".join(["erpnext", *requires, app])))
	write(
		root / "license.txt",
		"GNU General Public License v3.0 only. See the LICENSE file at the repository root.\n",
	)
	write(pkg / "__init__.py", '__version__ = "0.1.0"\n')
	write(pkg / "hooks.py", HOOKS.format(**fmt))
	write(pkg / "patches.txt", "[pre_model_sync]\n\n[post_model_sync]\n")
	write(pkg / "config" / "__init__.py", "")
	write(pkg / "public" / ".gitkeep", "")
	write(pkg / "templates" / "__init__.py", "")
	write(pkg / "templates" / "pages" / "__init__.py", "")
	write(root / "schema" / "__init__.py", "")

	modules_txt = pkg / "modules.txt"
	existing = modules_txt.read_text().split("\n") if modules_txt.exists() else []
	merged = [m for m in existing if m] + [m for m in modules if m not in existing]
	modules_txt.write_text("\n".join(merged) + "\n")
	for module in merged:
		module_dir = pkg / module.lower().replace(" ", "_")
		write(module_dir / "__init__.py", "")
	return 0


if __name__ == "__main__":
	sys.exit(main())
