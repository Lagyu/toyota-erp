"""Desk navigation as code (ERP-12): module sidebars, workspaces, number cards and
script reports, rendered to the JSON files Frappe imports on migrate.

A spec module may expose ``DESK: list[...]`` next to ``DOCTYPES``. Files land where
Frappe's module walk finds them:

    <app>/<module>/sidebar/<module>/<module>.json
    <app>/<module>/workspace/<name>/<name>.json
    <app>/<module>/number_card/<name>/<name>.json
    <app>/<module>/report/<name>/<name>.json      (the .py/.js are written by hand)
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from dsl import scrub

KPI_CARD_METHOD = "automotive_core.analytics.cards.kpi_card"


def _block_id(*parts: str) -> str:
	return hashlib.sha256("/".join(parts).encode()).hexdigest()[:10]  # stable block ids, not security


@dataclass
class Link:
	label: str
	link_to: str
	link_type: str = "DocType"  # DocType | Report | Page | Workspace | Dashboard | URL
	icon: str | None = None
	query_report: bool = False  # Script/Query Report links open the report view

	def workspace_row(self) -> dict[str, Any]:
		return {"hidden": 0, "is_query_report": int(self.query_report or self.link_type == "Report"),
			"label": self.label, "link_count": 0, "link_to": self.link_to, "link_type": self.link_type,
			"onboard": 0, "type": "Link"}

	def sidebar_row(self, child: bool = False) -> dict[str, Any]:
		row = {"child": int(child), "collapsible": 1, "indent": 0, "keep_closed": 0, "label": self.label,
			"link_to": self.link_to, "link_type": self.link_type, "open_in_new_tab": 0, "show_arrow": 0,
			"type": "Link"}
		if self.icon:
			row["icon"] = self.icon
		return row


@dataclass
class Group:
	label: str
	links: list[Link]
	icon: str | None = None

	def sidebar_rows(self) -> list[dict[str, Any]]:
		head = {"child": 0, "collapsible": 1, "indent": 1, "keep_closed": 1, "label": self.label,
			"link_type": "DocType", "open_in_new_tab": 0, "show_arrow": 0, "type": "Section Break"}
		if self.icon:
			head["icon"] = self.icon
		return [head, *[link.sidebar_row(child=True) for link in self.links]]


@dataclass
class NumberCard:
	"""A KPI card (value computed by automotive_core.analytics, scoped to the user's
	company/plant) or a document count card."""

	label: str
	module: str
	kpi: str | None = None
	document_type: str | None = None
	filters: list[list[Any]] = field(default_factory=list)
	color: str | None = None
	folder = "number_card"

	@property
	def name(self) -> str:
		return self.label

	def to_dict(self) -> dict[str, Any]:
		doc: dict[str, Any] = {"doctype": "Number Card", "name": self.label, "label": self.label, "module": self.module,
			"is_public": 1, "is_standard": 1, "show_percentage_stats": 0, "docstatus": 0, "idx": 0,
			"owner": "Administrator", "modified_by": "Administrator"}
		if self.kpi:
			doc.update({"type": "Custom", "method": KPI_CARD_METHOD,
				"filters_json": json.dumps({"kpi_code": self.kpi}, sort_keys=True)})
		elif self.document_type:
			doc.update({"type": "Document Type", "document_type": self.document_type, "function": "Count",
				"filters_json": json.dumps(self.filters)})
		else:
			raise ValueError(f"number card {self.label}: give a kpi or a document_type")
		if self.color:
			doc["color"] = self.color
		return doc


@dataclass
class ScriptReport:
	name: str
	module: str
	ref_doctype: str
	roles: list[str]
	add_total_row: bool = False
	prepared_report: bool = False
	folder = "report"

	def to_dict(self) -> dict[str, Any]:
		return {"doctype": "Report", "name": self.name, "report_name": self.name, "module": self.module,
			"ref_doctype": self.ref_doctype, "report_type": "Script Report", "is_standard": "Yes",
			"add_total_row": int(self.add_total_row), "prepared_report": int(self.prepared_report), "disabled": 0,
			"roles": [{"role": r} for r in self.roles], "docstatus": 0, "idx": 0, "owner": "Administrator",
			"modified_by": "Administrator", "query": "", "columns": [], "filters": []}


@dataclass
class Page:
	"""A desk page (route /desk/<name>); its <scrub(name)>.js is written by hand."""

	name: str
	title: str
	module: str
	roles: list[str]
	icon: str | None = None
	folder = "page"

	def to_dict(self) -> dict[str, Any]:
		return {"doctype": "Page", "name": self.name, "page_name": self.name, "title": self.title,
			"module": self.module, "standard": "Yes", "system_page": 0, "icon": self.icon or "",
			"roles": [{"role": r} for r in self.roles], "docstatus": 0, "idx": 0, "owner": "Administrator",
			"modified_by": "Administrator"}


@dataclass
class Workspace:
	"""A module's home page plus its sidebar (one per module)."""

	name: str
	module: str
	icon: str
	roles: list[str]
	number_cards: list[str] = field(default_factory=list)
	shortcuts: list[Link] = field(default_factory=list)
	groups: list[Group] = field(default_factory=list)
	sidebar: list[Link | Group] = field(default_factory=list)
	sequence: float = 50
	header_icon: str | None = None
	folder = "workspace"

	def to_dict(self) -> dict[str, Any]:
		content: list[dict[str, Any]] = [{"id": _block_id(self.name, "header"), "type": "header",
			"data": {"text": f'<span class="h4"><b>{self.name}</b></span>', "col": 12}}]
		content += [{"id": _block_id(self.name, "card", c), "type": "number_card",
			"data": {"number_card_name": c, "col": 3}} for c in self.number_cards]
		content += [{"id": _block_id(self.name, "shortcut", s.label), "type": "shortcut",
			"data": {"shortcut_name": s.label, "col": 3}} for s in self.shortcuts]
		content += [{"id": _block_id(self.name, "group", g.label), "type": "card",
			"data": {"card_name": g.label, "col": 4}} for g in self.groups]
		links: list[dict[str, Any]] = []
		for group in self.groups:
			links.append({"hidden": 0, "is_query_report": 0, "label": group.label, "link_count": len(group.links),
				"onboard": 0, "type": "Card Break"})
			links.extend(link.workspace_row() for link in group.links)
		return {
			"doctype": "Workspace", "name": self.name, "label": self.name, "title": self.name, "module": self.module,
			"icon": self.icon, "public": 1, "is_hidden": 0, "hide_custom": 0, "type": "Workspace", "standard": 1,
			"sequence_id": float(self.sequence), "for_user": "", "parent_page": "", "restrict_to_domain": "",
			"roles": [{"role": r} for r in self.roles], "content": json.dumps(content),
			"number_cards": [{"label": c, "number_card_name": c} for c in self.number_cards],
			"shortcuts": [{"label": s.label, "link_to": s.link_to, "type": s.link_type if s.link_type != "Report"
				else "Report", "color": "Grey", "doc_view": "List" if s.link_type == "DocType" else ""}
				for s in self.shortcuts],
			"links": links, "charts": [], "quick_lists": [], "custom_blocks": [],
			"docstatus": 0, "idx": 0, "owner": "Administrator", "modified_by": "Administrator",
		}

	def sidebar_dict(self) -> dict[str, Any]:
		items = [Link("Home", self.name, "Workspace", icon="house").sidebar_row()]
		for entry in self.sidebar:
			items.extend(entry.sidebar_rows() if isinstance(entry, Group) else [entry.sidebar_row()])
		return {"doctype": "Sidebar", "name": self.module, "module": self.module, "title": self.module,
			"header_icon": self.header_icon or self.icon, "standard": 1, "items": items, "docstatus": 0, "idx": 0,
			"owner": "Administrator", "modified_by": "Administrator"}


def records(spec) -> list[tuple[str, str, str, dict[str, Any]]]:
	"""(module, folder, file stem, document) for every file a desk spec produces."""
	out = []
	if isinstance(spec, Workspace):
		out.append((spec.module, "workspace", scrub(spec.name), spec.to_dict()))
		out.append((spec.module, "sidebar", scrub(spec.module), spec.sidebar_dict()))
	else:
		out.append((spec.module, spec.folder, scrub(spec.name), spec.to_dict()))
	return out
