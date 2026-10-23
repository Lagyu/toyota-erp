"""Configuration rule evaluation (requires / excludes / allowed values). Pure functions."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
	code: str
	rule_type: str  # "Requires" | "Excludes" | "Allowed Values"
	then_attribute: str
	then_values: frozenset[str]
	if_attribute: str | None = None
	if_values: frozenset[str] = field(default_factory=frozenset)
	message: str | None = None

	def applies(self, selections: dict[str, str]) -> bool:
		if not self.if_attribute:
			return True
		return selections.get(self.if_attribute) in self.if_values


@dataclass(frozen=True)
class Violation:
	rule: str
	attribute: str
	message: str


def evaluate(selections: dict[str, str], rules: list[Rule], required: list[str] | None = None,
		allowed_values: dict[str, set[str]] | None = None) -> list[Violation]:
	"""Return every violation (empty list = valid). Deterministic order (rule code)."""
	violations: list[Violation] = []
	for attribute in required or []:
		if not selections.get(attribute):
			violations.append(Violation("required", attribute, f"{attribute} must be selected"))
	for attribute, value in sorted(selections.items()):
		domain = (allowed_values or {}).get(attribute)
		if domain is not None and value not in domain:
			violations.append(Violation("domain", attribute, f"{value} is not a valid {attribute}"))
	for rule in sorted(rules, key=lambda r: r.code):
		if not rule.applies(selections):
			continue
		value = selections.get(rule.then_attribute)
		if rule.rule_type in ("Requires", "Allowed Values"):
			ok = value in rule.then_values
		elif rule.rule_type == "Excludes":
			ok = value not in rule.then_values
		else:
			raise ValueError(f"unknown rule type {rule.rule_type}")
		if not ok:
			default = (
				f"{rule.then_attribute} must be one of {', '.join(sorted(rule.then_values))}"
				if rule.rule_type != "Excludes"
				else f"{rule.then_attribute} {value} is not available with this configuration"
			)
			violations.append(Violation(rule.code, rule.then_attribute, rule.message or default))
	return violations


def configuration_key(model: str, selections: dict[str, str]) -> str:
	return model + "|" + "|".join(f"{k}={v}" for k, v in sorted(selections.items()))
