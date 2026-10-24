"""Vehicle configuration: validate option selections against compatibility rules
and resolve them to a buildable variant item (ERP-02, ERP-08 configuration check)."""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime, nowdate

from automotive_manufacturing.engine import options


def _split(text: str | None) -> frozenset[str]:
	return frozenset(v.strip() for v in (text or "").splitlines() if v.strip())


def rules_for(model: str, on_date=None) -> list[options.Rule]:
	day = getdate(on_date or nowdate())
	rows = frappe.get_all(
		"Vehicle Option Rule",
		filters={"vehicle_model": model, "is_active": 1},
		fields=[
			"rule_code",
			"rule_type",
			"if_attribute",
			"if_values",
			"then_attribute",
			"then_values",
			"message",
			"effective_from",
			"effective_to",
		],
	)
	rules = []
	for r in rows:
		if r.effective_from and getdate(r.effective_from) > day:
			continue
		if r.effective_to and getdate(r.effective_to) < day:
			continue
		rules.append(
			options.Rule(
				r.rule_code,
				r.rule_type,
				r.then_attribute,
				_split(r.then_values),
				r.if_attribute or None,
				_split(r.if_values),
				r.message or None,
			)
		)
	return rules


def model_attributes(model: str) -> tuple[list[str], list[str], dict[str, set[str]]]:
	"""(all attributes in display order, required attributes, value domains)."""
	doc = frappe.get_cached_doc("Vehicle Model", model)
	groups = sorted(doc.option_groups, key=lambda g: (g.display_order or 0, g.idx))
	attributes = [g.attribute for g in groups]
	required = [g.attribute for g in groups if g.required]
	domains = {
		a: set(frappe.get_all("Item Attribute Value", filters={"parent": a}, pluck="attribute_value"))
		for a in attributes
	}
	return attributes, required, domains


def check_selections(model: str, selections: dict[str, str], on_date=None) -> dict[str, Any]:
	attributes, required, domains = model_attributes(model)
	unknown = sorted(set(selections) - set(attributes))
	violations = [
		options.Violation("unknown", a, _("{0} is not an option of {1}").format(a, model)) for a in unknown
	]
	violations += options.evaluate(selections, rules_for(model, on_date), required, domains)
	return {
		"valid": not violations,
		"violations": [{"rule": v.rule, "attribute": v.attribute, "message": v.message} for v in violations],
		"key": options.configuration_key(model, selections),
	}


def find_variant(template: str, selections: dict[str, str]) -> str | None:
	"""The variant of ``template`` whose attributes equal ``selections`` exactly.

	Resolution is a system lookup (dealers configure through the portal without read
	access to the item master), so it does not go through ERPNext's permission-checked
	get_variant."""
	candidates = None
	for attribute, value in sorted(selections.items()):
		rows = set(
			frappe.db.sql_list(
				"""select iva.parent from `tabItem Variant Attribute` iva join `tabItem` i on i.name = iva.parent
			where i.variant_of = %s and iva.attribute = %s and iva.attribute_value = %s and i.disabled = 0""",
				(template, attribute, value),
			)
		)
		candidates = rows if candidates is None else candidates & rows
		if not candidates:
			return None
	for name in sorted(candidates or []):
		if frappe.db.count("Item Variant Attribute", {"parent": name}) == len(selections):
			return name
	return None


def resolve_variant(model: str, selections: dict[str, str]) -> str:
	"""Existing variant item for the selections, or a new one created from the template."""
	from erpnext.controllers.item_variant import create_variant

	template = frappe.get_cached_value("Vehicle Model", model, "template_item")
	existing = find_variant(template, selections)
	if existing:
		return existing
	variant = create_variant(template, selections)
	variant.automotive_vehicle_model = model
	variant.flags.ignore_permissions = True
	variant.insert()
	return variant.name


@frappe.whitelist()
def check(vehicle_model: str, selections: str | dict) -> dict[str, Any]:
	"""Configurator API (desk and dealer portal): validity and violations without side effects."""
	frappe.has_permission("Vehicle Model", "read", doc=vehicle_model, throw=True)
	if isinstance(selections, str):
		selections = json.loads(selections)
	result = check_selections(vehicle_model, {k: str(v) for k, v in selections.items()})
	if result["valid"]:
		template = frappe.get_cached_value("Vehicle Model", vehicle_model, "template_item")
		result["variant_item"] = find_variant(template, selections)
	return result


def apply_to_document(doc) -> None:
	"""Vehicle Configuration.validate: compute key, status, violations and variant."""
	selections = {s.attribute: s.attribute_value for s in doc.selections}
	if len(selections) != len(doc.selections):
		frappe.throw(_("An attribute is selected twice."))
	result = check_selections(doc.vehicle_model, selections)
	doc.configuration_key = result["key"]
	doc.validated_at = now_datetime()
	if result["valid"]:
		doc.status = "Valid"
		doc.violations = None
		doc.variant_item = resolve_variant(doc.vehicle_model, selections)
	else:
		doc.status = "Invalid"
		doc.variant_item = None
		doc.violations = "\n".join(v["message"] for v in result["violations"])


@frappe.whitelist(methods=["POST"])
def configure(vehicle_model: str, selections: str | dict) -> dict[str, Any]:
	"""Create (or reuse) a valid configuration; raise if the selections are invalid."""
	if isinstance(selections, str):
		selections = json.loads(selections)
	key = options.configuration_key(vehicle_model, selections)
	existing = frappe.db.get_value(
		"Vehicle Configuration",
		{"configuration_key": key, "status": "Valid"},
		["name", "variant_item"],
		as_dict=True,
	)
	if existing:
		return {"configuration": existing.name, "variant_item": existing.variant_item}
	doc = frappe.get_doc(
		{
			"doctype": "Vehicle Configuration",
			"vehicle_model": vehicle_model,
			"selections": [{"attribute": a, "attribute_value": v} for a, v in sorted(selections.items())],
		}
	)
	doc.insert()
	if doc.status != "Valid":
		frappe.throw(_("Invalid configuration: {0}").format(doc.violations))
	return {"configuration": doc.name, "variant_item": doc.variant_item}
