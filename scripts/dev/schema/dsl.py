"""Compact schema-as-code DSL for the custom apps' DocTypes.

Each app keeps its DocType definitions in ``apps/<app>/schema/*.py`` (outside the
Python package, so they are not shipped). ``generate.py`` renders them into the
DocType JSON files that Frappe syncs on ``bench migrate``. The specs are the
canonical source: the JSON files are generated artefacts and CI fails when they
drift (``generate.py --check``). Desk-side DocType editing is not used for these
DocTypes (developer mode is off outside local development).

Only non-default properties are emitted, which keeps the JSON diff-friendly and
lets Frappe fill in its own defaults.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

LAYOUT_TYPES = {"Section Break", "Column Break", "Tab Break"}

# Field properties that may be passed as keyword arguments, in emission order.
FIELD_PROPS = (
	"options",
	"default",
	"reqd",
	"unique",
	"read_only",
	"hidden",
	"bold",
	"in_list_view",
	"in_standard_filter",
	"in_preview",
	"search_index",
	"allow_on_submit",
	"no_copy",
	"print_hide",
	"report_hide",
	"permlevel",
	"depends_on",
	"mandatory_depends_on",
	"read_only_depends_on",
	"fetch_from",
	"fetch_if_empty",
	"description",
	"length",
	"precision",
	"ignore_user_permissions",
	"set_only_once",
	"translatable",
	"collapsible",
	"collapsible_depends_on",
	"columns",
	"non_negative",
	"sort_options",
	"link_filters",
	"in_global_search",
	"allow_in_quick_entry",
	"is_virtual",
	"max_height",
	"show_dashboard",
)


@dataclass
class Field:
	fieldname: str | None
	fieldtype: str
	label: str | None = None
	props: dict[str, Any] = field(default_factory=dict)

	def to_dict(self) -> dict[str, Any]:
		out: dict[str, Any] = {"fieldname": self.fieldname, "fieldtype": self.fieldtype}
		if self.label:
			out["label"] = self.label
		for key in FIELD_PROPS:
			if key in self.props and self.props[key] not in (None, False, 0, ""):
				value = self.props[key]
				out[key] = 1 if value is True else value
		unknown = set(self.props) - set(FIELD_PROPS)
		if unknown:
			raise ValueError(f"unknown field properties for {self.fieldname}: {sorted(unknown)}")
		return out


def _f(fieldtype: str):
	def make(fieldname: str, label: str | None = None, **props: Any) -> Field:
		return Field(fieldname, fieldtype, label, props)

	make.__name__ = fieldtype.lower().replace(" ", "_")
	return make


data = _f("Data")
int_ = _f("Int")
float_ = _f("Float")
currency = _f("Currency")
percent = _f("Percent")
check = _f("Check")
date = _f("Date")
datetime = _f("Datetime")
time = _f("Time")
duration = _f("Duration")
small_text = _f("Small Text")
text = _f("Text")
long_text = _f("Long Text")
text_editor = _f("Text Editor")
code = _f("Code")
json_ = _f("JSON")
attach = _f("Attach")
attach_image = _f("Attach Image")
password = _f("Password")
read_only = _f("Read Only")
html = _f("HTML")
button = _f("Button")
barcode = _f("Barcode")
signature = _f("Signature")
rating = _f("Rating")


def link(fieldname: str, label: str, doctype: str, **props: Any) -> Field:
	return Field(fieldname, "Link", label, {"options": doctype, **props})


def dynamic_link(fieldname: str, label: str, doctype_field: str, **props: Any) -> Field:
	return Field(fieldname, "Dynamic Link", label, {"options": doctype_field, **props})


def select(fieldname: str, label: str, choices: list[str], **props: Any) -> Field:
	return Field(fieldname, "Select", label, {"options": "\n".join(choices), **props})


def table(fieldname: str, label: str, child_doctype: str, **props: Any) -> Field:
	return Field(fieldname, "Table", label, {"options": child_doctype, **props})


def table_multiselect(fieldname: str, label: str, child_doctype: str, **props: Any) -> Field:
	return Field(fieldname, "Table MultiSelect", label, {"options": child_doctype, **props})


def section(label: str | None = None, fieldname: str | None = None, **props: Any) -> Field:
	return Field(fieldname, "Section Break", label, props)


def column(fieldname: str | None = None, **props: Any) -> Field:
	return Field(fieldname, "Column Break", None, props)


def tab(label: str, fieldname: str | None = None, **props: Any) -> Field:
	return Field(fieldname, "Tab Break", label, props)


def amended_from(doctype: str) -> Field:
	return Field(
		"amended_from",
		"Link",
		"Amended From",
		{"options": doctype, "no_copy": 1, "print_hide": 1, "read_only": 1, "search_index": 1},
	)


def naming_series(options: str | list[str], default: str | None = None) -> Field:
	choices = [options] if isinstance(options, str) else options
	return Field(
		"naming_series",
		"Select",
		"Series",
		{
			"options": "\n".join(choices),
			"default": default or choices[0],
			"reqd": 1,
			"no_copy": 1,
			"print_hide": 1,
			"set_only_once": 1,
		},
	)


RIGHTS = {
	"r": "read",
	"w": "write",
	"c": "create",
	"d": "delete",
	"s": "submit",
	"x": "cancel",
	"a": "amend",
	"p": "report",
	"e": "export",
	"i": "import",
	"h": "share",
	"t": "print",
	"m": "email",
	"o": "if_owner",
	"l": "select",
}


def perm(role: str, rights: str = "r", permlevel: int = 0) -> dict[str, Any]:
	"""perm("Buyer", "rwc pe") -> DocPerm dict. Letters: see RIGHTS; spaces ignored.

	Read implies print: a print format shows only what the role can already read, and the
	desk offers Print only to roles with the print right."""
	out: dict[str, Any] = {"role": role}
	for letter in rights.replace(" ", ""):
		if letter not in RIGHTS:
			raise ValueError(f"unknown right {letter!r} in {rights!r}")
		out[RIGHTS[letter]] = 1
	if out.get("read") and not permlevel:
		out["print"] = 1
	if permlevel:
		out["permlevel"] = permlevel
	return out


@dataclass
class DocType:
	name: str
	module: str
	fields: list[Field]
	autoname: str | None = None
	submittable: bool = False
	child: bool = False
	single: bool = False
	track_changes: bool = True
	title_field: str | None = None
	search_fields: str | None = None
	permissions: list[dict[str, Any]] = field(default_factory=list)
	description: str | None = None
	icon: str | None = None
	quick_entry: bool = False
	allow_import: bool = False
	allow_rename: bool = False
	sort_field: str = "creation"
	sort_order: str = "DESC"
	editable_grid: bool = True
	image_field: str | None = None
	max_attachments: int | None = None
	show_title_field_in_link: bool = False
	track_seen: bool = False
	is_tree: bool = False
	nsm_parent_field: str | None = None
	links: list[dict[str, Any]] = field(default_factory=list)
	extra: dict[str, Any] = field(default_factory=dict)

	@property
	def scrubbed(self) -> str:
		return scrub(self.name)

	def to_dict(self) -> dict[str, Any]:
		fields = [f.to_dict() for f in self._named_fields()]
		names = [f["fieldname"] for f in fields]
		duplicates = {n for n in names if names.count(n) > 1}
		if duplicates:
			raise ValueError(f"{self.name}: duplicate fieldnames {sorted(duplicates)}")
		for fieldname in filter(None, [self.title_field, self.image_field]):
			if fieldname not in names:
				raise ValueError(f"{self.name}: title/image field {fieldname} is not defined")
		if self.submittable and "amended_from" not in names:
			raise ValueError(f"{self.name}: submittable DocTypes need an amended_from field")

		out: dict[str, Any] = {
			"actions": [],
			"doctype": "DocType",
			"engine": "InnoDB",
			"field_order": names,
			"fields": fields,
			"links": self.links,
			"module": self.module,
			"name": self.name,
			"owner": "Administrator",
			"modified_by": "Administrator",
			"permissions": [] if self.child else self.permissions,
			"sort_field": self.sort_field,
			"sort_order": self.sort_order,
			"states": [],
		}
		if self.autoname:
			out["autoname"] = self.autoname
			out["naming_rule"] = naming_rule(self.autoname)
		flags = {
			"is_submittable": self.submittable,
			"istable": self.child,
			"issingle": self.single,
			"track_changes": self.track_changes and not self.child,
			"quick_entry": self.quick_entry,
			"allow_import": self.allow_import,
			"allow_rename": self.allow_rename,
			"editable_grid": self.editable_grid,
			"show_title_field_in_link": self.show_title_field_in_link,
			"track_seen": self.track_seen,
			"is_tree": self.is_tree,
		}
		out.update({k: 1 for k, v in flags.items() if v})
		for key, value in (
			("title_field", self.title_field),
			("search_fields", self.search_fields),
			("description", self.description),
			("icon", self.icon),
			("image_field", self.image_field),
			("max_attachments", self.max_attachments),
			("nsm_parent_field", self.nsm_parent_field),
		):
			if value:
				out[key] = value
		out.update(self.extra)
		if not self.child and not self.permissions:
			raise ValueError(f"{self.name}: non-child DocTypes must declare permissions (default deny)")
		return out

	def _named_fields(self) -> list[Field]:
		counters: dict[str, int] = {}
		named = []
		for f in self.fields:
			if f.fieldname is None:
				if f.fieldtype not in LAYOUT_TYPES:
					raise ValueError(f"{self.name}: {f.fieldtype} field without fieldname")
				prefix = scrub(f.fieldtype)
				counters[prefix] = counters.get(prefix, 0) + 1
				f = Field(f"{prefix}_{counters[prefix]}", f.fieldtype, f.label, f.props)
			named.append(f)
		return named


def naming_rule(autoname: str) -> str:
	if autoname.startswith("field:"):
		return "By fieldname"
	if autoname.startswith("naming_series:"):
		return 'By "Naming Series" field'
	if autoname.startswith("format:"):
		return "Expression"
	if autoname == "hash":
		return "Random"
	if autoname == "autoincrement":
		return "Autoincrement"
	if autoname.lower() == "prompt":
		return "Set by user"
	return "Expression (old style)"


def scrub(text: str) -> str:
	return re.sub(r"[^a-z0-9_]+", "_", text.strip().lower().replace("-", "_")).strip("_")


def class_name(doctype: str) -> str:
	# Same rule as frappe.model.base_document.get_controller
	return doctype.replace(" ", "").replace("-", "")
