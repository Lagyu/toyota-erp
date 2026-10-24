#!/usr/bin/env python3
"""OpenAPI 3.1 contract of the custom HTTP methods (ERP-11), generated from the code.

Every ``@frappe.whitelist`` function of the production apps becomes an operation on
``/api/method/<module>.<function>``: HTTP verbs, guest access, parameters and their types
(from annotations and defaults), the ``Idempotency-Key`` header of ``@idempotent``
methods, the roles checked with ``frappe.only_for`` (``x-roles``), the partner scope of
portal methods (``x-party``) and the response shape: dict literals and ``frappe.get_all``
field lists give the properties of ``message``; otherwise the return annotation does.

The spec is written to apps/automotive_core/automotive_core/integration_hub/openapi.json
(part of the app; partners get it with ops/runbooks/integration-operations.md, and the API
contract tests in integration_tests/ check the running stack against it); ``--check`` fails
when it is out of date (CI lint). Pure ``ast``: no Frappe import needed.

``--compat OLD.json`` compares the contract with an earlier one (CI: the previous commit's)
and enforces the versioning policy of DECISIONS.md D-034: a breaking change needs a new
major version of ``info.version``, an addition a new minor one.

	python3 scripts/dev/openapi.py [--check | --compat OLD.json]
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPS = ("automotive_core", "automotive_manufacturing", "automotive_supply_chain", "automotive_portals")
OUT = ROOT / "apps/automotive_core/automotive_core/integration_hub/openapi.json"
# semantic version of the contract (D-034): bump it with every change of the operations
VERSION = "1.1.0"
PARTY_HELPERS = {"_supplier": "Supplier", "_dealer": "Customer"}


def _const(node, names: dict) -> object:
	"""Literal value of a node (strings, tuples/lists of strings, module-level names)."""
	if isinstance(node, ast.Constant):
		return node.value
	if isinstance(node, ast.Tuple | ast.List):
		values = [_const(e, names) for e in node.elts]
		flat = []
		for v in values:
			flat.extend(v if isinstance(v, list) else [v])
		return flat
	if isinstance(node, ast.Name) and node.id in names:
		return names[node.id]
	if isinstance(node, ast.Starred):
		return _const(node.value, names)
	return None


def _type_schema(annotation) -> tuple[dict, bool]:
	"""(schema, nullable) for a parameter/return annotation."""
	if annotation is None:
		return {}, False
	text = ast.unparse(annotation).replace(" ", "")
	parts = [p for p in text.split("|") if p]
	nullable = "None" in parts
	parts = [p for p in parts if p != "None"]

	def one(t: str) -> dict:
		base = t.split("[", 1)[0]
		return {
			"str": {"type": "string"},
			"int": {"type": "integer"},
			"float": {"type": "number"},
			"bool": {"type": "boolean"},
			"dict": {"type": "object"},
			"list": {"type": "array"},
			"Any": {},
		}.get(base, {})

	schemas = [one(p) for p in parts]
	if not schemas:
		return ({"type": "null"} if nullable else {}), nullable
	if len(schemas) == 1:
		schema = dict(schemas[0])
	else:
		# "list | str" / "dict | str": JSON value or its JSON-encoded string (form posts)
		schema = {"oneOf": schemas} if all(schemas) else {}
	if text.startswith("list[dict") or text.startswith("list[dict[str"):
		schema = {"type": "array", "items": {"type": "object"}}
	return schema, nullable


def _object(keys: list[str]) -> dict:
	return {"type": "object", "properties": {k: {} for k in keys}, "required": list(keys)}


def _return_schema(fn: ast.FunctionDef) -> dict:
	shapes = []
	for node in ast.walk(fn):
		if not isinstance(node, ast.Return) or node.value is None:
			continue
		value = node.value
		if isinstance(value, ast.Dict) and all(isinstance(k, ast.Constant) for k in value.keys):
			shapes.append(_object([k.value for k in value.keys]))
		elif isinstance(value, ast.Call) and ast.unparse(value.func) == "frappe.get_all":
			fields = next((kw.value for kw in value.keywords if kw.arg == "fields"), None)
			if isinstance(fields, ast.List) and all(isinstance(e, ast.Constant) for e in fields.elts):
				names = [e.value.split(" as ")[-1].strip() for e in fields.elts]
				shapes.append(
					{"type": "array", "items": {"type": "object", "properties": {n: {} for n in names}}}
				)
	nested = [n for n in ast.walk(fn) if isinstance(n, ast.FunctionDef | ast.Lambda) and n is not fn]
	if shapes and not nested and all(s == shapes[0] for s in shapes):
		return shapes[0]
	schema, nullable = _type_schema(fn.returns)
	return schema if not nullable or schema else {"type": "null"}


def _operation(module: str, fn: ast.FunctionDef, deco: ast.expr, names: dict) -> tuple[str, dict, dict]:
	allow_guest, methods = False, ["GET", "POST"]
	if isinstance(deco, ast.Call):
		for kw in deco.keywords:
			if kw.arg == "allow_guest":
				allow_guest = bool(_const(kw.value, names))
			if kw.arg == "methods":
				methods = [m.upper() for m in _const(kw.value, names)]
	idempotency = None
	for d in fn.decorator_list:
		if isinstance(d, ast.Call) and ast.unparse(d.func).endswith("idempotent"):
			required = next(
				(bool(_const(kw.value, names)) for kw in d.keywords if kw.arg == "required"), True
			)
			idempotency = {"endpoint": _const(d.args[0], names) if d.args else None, "required": required}
	roles, party = [], None
	for node in ast.walk(fn):
		if isinstance(node, ast.Call):
			name = ast.unparse(node.func)
			if name == "frappe.only_for" and node.args:
				value = _const(node.args[0], names)
				roles = value if isinstance(value, list) else [value] if value else roles
			if name in PARTY_HELPERS:
				party = PARTY_HELPERS[name]
			if name in ("require_party", "require_party_access") and node.args:
				party = _const(node.args[0], names) or party
	params, required = {}, []
	args = fn.args.args
	defaults = [None] * (len(args) - len(fn.args.defaults)) + list(fn.args.defaults)
	for arg, default in zip(args, defaults, strict=True):
		if arg.arg in ("self", "cls"):
			continue
		schema, nullable = _type_schema(arg.annotation)
		if default is not None:
			value = _const(default, names)
			if value is not None and not isinstance(value, list):
				schema = {**schema, "default": value}
		elif not nullable:
			required.append(arg.arg)
		if nullable and schema:
			schema = {"oneOf": [schema, {"type": "null"}]} if "oneOf" not in schema else schema
		params[arg.arg] = schema
	doc = (ast.get_docstring(fn) or "").strip()
	summary = doc.split("\n\n")[0].replace("\n", " ") if doc else fn.name.replace("_", " ")
	# a path kept for a release after its replacement (D-034): "Deprecated: use <path> ..."
	deprecation = next(
		(
			line.strip()[len("Deprecated:") :].strip()
			for line in doc.splitlines()
			if line.strip().startswith("Deprecated:")
		),
		None,
	)
	operation: dict = {
		"operationId": f"{module}.{fn.name}",
		"summary": summary[:300],
		"tags": [module.rsplit(".", 1)[0]],
		"responses": {
			"200": {
				"description": "OK",
				"content": {
					"application/json": {
						"schema": {"type": "object", "properties": {"message": _return_schema(fn)}}
					}
				},
			},
			"403": {"$ref": "#/components/responses/Forbidden"},
			"404": {"$ref": "#/components/responses/NotFound"},
			"417": {"$ref": "#/components/responses/ValidationError"},
		},
	}
	if deprecation is not None:
		operation["deprecated"] = True
		operation["x-deprecation"] = deprecation
	if allow_guest:
		operation["security"] = []
	if roles:
		operation["x-roles"] = sorted(set(roles))
	if party:
		operation["x-party"] = party
	header_params = []
	if idempotency:
		operation["x-idempotency-endpoint"] = idempotency["endpoint"]
		header_params.append(
			{
				"$ref": "#/components/parameters/IdempotencyKey"
				if idempotency["required"]
				else "#/components/parameters/IdempotencyKeyOptional"
			}
		)
		operation["responses"]["409"] = {"$ref": "#/components/responses/InProgress"}
		operation["responses"]["422"] = {"$ref": "#/components/responses/IdempotencyConflict"}
	header_params.append({"$ref": "#/components/parameters/CorrelationId"})
	body = {"type": "object", "properties": params}
	if required:
		body["required"] = required
	ops = {}
	for method in methods:
		op = dict(
			operation,
			operationId=f"{operation['operationId']}"
			+ ("" if method == methods[-1] else f".{method.lower()}"),
		)
		op["parameters"] = list(header_params)
		if method in ("GET", "DELETE"):
			op["parameters"] += [
				{"name": n, "in": "query", "required": n in required, "schema": s} for n, s in params.items()
			]
		else:
			op["requestBody"] = {
				"required": bool(required),
				"content": {
					"application/json": {"schema": body},
					"application/x-www-form-urlencoded": {"schema": body},
				},
			}
		ops[method.lower()] = op
	return f"/api/method/{module}.{fn.name}", ops, {}


def _module_names(tree: ast.Module) -> dict:
	names = {}
	for node in tree.body:
		if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
			value = _const(node.value, names)
			if value is not None:
				names[node.targets[0].id] = value
	return names


def _message_types() -> list[str]:
	tree = ast.parse((ROOT / "apps/automotive_core/automotive_core/integration_hub/contracts.py").read_text())
	return _module_names(tree)["INBOUND_MESSAGE_TYPES"]


INBOUND = "/api/method/automotive_core.integration_hub.inbound.receive"


def _inbound_contract(ops: dict) -> dict:
	"""The partner endpoint authenticates by signature and carries the message envelope."""
	op = ops["post"]
	op["security"] = [{"partnerSignature": []}]
	op["parameters"] = [
		{"name": "X-Hinode-Partner", "in": "header", "required": True, "schema": {"type": "string"}},
		*op["parameters"],
	]
	op["requestBody"] = {
		"required": True,
		"content": {
			"application/json": {"schema": {"$ref": "#/components/schemas/InboundEnvelope"}},
			"text/plain": {
				"schema": {
					"type": "string",
					"description": "The same envelope in Hinode EDI segments (partners configured for EDI)",
				}
			},
		},
	}
	op["responses"]["200"] = {
		"description": "Duplicate delivery: the stored result",
		"content": {
			"application/json": {
				"schema": {
					"type": "object",
					"properties": {"message": {"$ref": "#/components/schemas/AcceptedResponse"}},
				}
			}
		},
	}
	op["responses"]["202"] = {
		"description": "Accepted for processing",
		"content": {
			"application/json": {
				"schema": {
					"type": "object",
					"properties": {"message": {"$ref": "#/components/schemas/AcceptedResponse"}},
				}
			}
		},
	}
	op["responses"]["401"] = {"description": "Unknown partner, bad or replayed signature"}
	op["responses"]["429"] = {"description": "Partner rate limit exceeded"}
	return {"post": op}


def _request_limits() -> tuple[tuple, tuple]:
	"""The method prefixes the per-user request limits apply to (access/request_budgets.py)."""
	tree = ast.parse((ROOT / "apps/automotive_core/automotive_core/access/request_budgets.py").read_text())
	names = _module_names(tree)
	return tuple(names["_PORTAL"]), tuple(names["_SHOP_FLOOR"])


def _limit(ops: dict, method_path: str, portal: tuple, shop_floor: tuple) -> dict:
	"""Mark the operations a request limit applies to, and their 429 answer."""
	for verb, op in ops.items():
		if method_path.startswith(portal):
			group = "portal-read" if verb in ("get", "head") else "portal-change"
		elif method_path.startswith(shop_floor):
			group = "shop-floor"
		else:
			continue
		op["x-request-limit"] = group
		op["responses"] = {**op["responses"], "429": {"$ref": "#/components/responses/TooManyRequests"}}
	return ops


def build() -> dict:
	paths: dict = {}
	portal, shop_floor = _request_limits()
	for app in APPS:
		base = ROOT / "apps" / app
		for path in sorted((base / app).rglob("*.py")):
			rel = path.relative_to(base)
			if "tests" in rel.parts or "patches" in rel.parts or rel.name.startswith("test_"):
				continue
			tree = ast.parse(path.read_text(), filename=str(path))
			names = _module_names(tree)
			module = ".".join(rel.with_suffix("").parts)
			for fn in tree.body:
				if not isinstance(fn, ast.FunctionDef):
					continue
				deco = next(
					(
						d
						for d in fn.decorator_list
						if ast.unparse(d.func if isinstance(d, ast.Call) else d) == "frappe.whitelist"
					),
					None,
				)
				if deco is None:
					continue
				route, ops, _ = _operation(module, fn, deco, names)
				ops = _limit(ops, f"{module}.{fn.name}", portal, shop_floor)
				paths[route] = _inbound_contract(ops) if route == INBOUND else ops
	return {
		"openapi": "3.1.0",
		"info": {
			"title": "Hinode Automotive ERP — custom methods",
			"version": VERSION,
			"description": (
				"Whitelisted methods of the custom apps, called as Frappe methods: "
				"`POST /api/method/<path>` (JSON or form body) or `GET` with query parameters where "
				"allowed; the result is in `message`. Authenticate with a session (portal and desk "
				"users; send the CSRF token in `X-Frappe-CSRF-Token`) or an API key "
				"(`Authorization: token <key>:<secret>`). `x-roles` lists the roles a method requires; "
				"`x-party` marks partner-portal methods scoped to the caller's own supplier or dealer. "
				"State-changing partner and MES methods require an `Idempotency-Key`; `x-request-limit` "
				"names the per-user limit a portal or shop-floor method counts against. Partner systems "
				"send messages to the signed inbound endpoint "
				"(`automotive_core.integration_hub.inbound.receive`). Versions: a method path keeps its "
				"behaviour within a major version; a breaking change comes as a new path, and the old "
				"one stays, marked deprecated, for one more release. Generated by scripts/dev/openapi.py."
			),
		},
		"servers": [{"url": "/"}],
		"security": [{"session": []}, {"apiToken": []}],
		"paths": dict(sorted(paths.items())),
		"components": {
			"securitySchemes": {
				"session": {"type": "apiKey", "in": "cookie", "name": "sid"},
				"apiToken": {
					"type": "apiKey",
					"in": "header",
					"name": "Authorization",
					"description": "token <api_key>:<api_secret>",
				},
				"partnerSignature": {
					"type": "apiKey",
					"in": "header",
					"name": "X-Hinode-Signature",
					"description": "t=<unix seconds>,kid=<key id>,n=<nonce>,v1=<hex HMAC-SHA256(secret, t + '.' + n + '.' + body)>"
					" with X-Hinode-Partner: <partner code>",
				},
			},
			"parameters": {
				"IdempotencyKey": {
					"name": "Idempotency-Key",
					"in": "header",
					"required": True,
					"schema": {"type": "string", "maxLength": 120},
					"description": "Unique per business request; a retry with the same key returns the first result.",
				},
				"IdempotencyKeyOptional": {
					"name": "Idempotency-Key",
					"in": "header",
					"required": False,
					"schema": {"type": "string", "maxLength": 120},
				},
				"CorrelationId": {
					"name": "X-Correlation-ID",
					"in": "header",
					"required": False,
					"schema": {"type": "string", "pattern": "^[A-Za-z0-9._:-]{8,64}$"},
					"description": "Echoed in the response; follows the request into jobs and messages.",
				},
			},
			"schemas": {
				"InboundEnvelope": {
					"type": "object",
					"additionalProperties": False,
					"required": ["message_id", "message_type", "data"],
					"properties": {
						"message_id": {
							"type": "string",
							"minLength": 1,
							"maxLength": 100,
							"pattern": "^[A-Za-z0-9._:-]+$",
							"description": "Unique per partner; a redelivery with the same id is a duplicate",
						},
						"message_type": {"type": "string", "enum": _message_types()},
						"schema_version": {"type": "string", "enum": ["v1"], "default": "v1"},
						"sent_at": {"type": ["string", "null"], "format": "date-time"},
						"data": {
							"type": "object",
							"description": "Validated by the handler of the message type",
						},
					},
				},
				"AcceptedResponse": {
					"type": "object",
					"required": ["status", "inbound_id", "processing_status"],
					"properties": {
						"status": {"type": "string", "enum": ["accepted", "duplicate"]},
						"inbound_id": {"type": "string"},
						"processing_status": {"type": "string"},
						"result_doctype": {"type": ["string", "null"]},
						"result_name": {"type": ["string", "null"]},
					},
				},
			},
			"responses": {
				"Forbidden": {"description": "Not permitted for this user, role or partner"},
				"NotFound": {"description": "A document the request names does not exist"},
				"ValidationError": {
					"description": "Business rule violated; the reason is in `_server_messages`"
				},
				"InProgress": {"description": "The same Idempotency-Key is being processed"},
				"IdempotencyConflict": {"description": "Idempotency-Key reused for a different request"},
				"TooManyRequests": {
					"description": "Over the caller's request limit (`x-request-limit`, per user "
					"and minute, set in Automotive Settings); retry in the next minute"
				},
			},
		},
	}


# ---------------------------------------------------------------------------------------
# Compatibility with an earlier contract (D-034)
# ---------------------------------------------------------------------------------------


def _plain(schema, drop=("description", "default")):
	"""A schema without what does not change the wire format (descriptions, defaults)."""
	if isinstance(schema, dict):
		return {k: _plain(v, drop) for k, v in schema.items() if k not in drop}
	if isinstance(schema, list):
		return [_plain(v, drop) for v in schema]
	return schema


def _inputs(spec: dict, op: dict) -> dict:
	"""name -> (schema, required) of an operation's query, header and body parameters."""
	inputs = {}
	for param in op.get("parameters", []):
		if "$ref" in param:
			param = spec["components"]["parameters"][param["$ref"].rsplit("/", 1)[-1]]
		inputs[f"{param['in']}:{param['name']}"] = (param.get("schema"), bool(param.get("required")))
	body = op.get("requestBody")
	if body:
		schema = next(iter(body["content"].values()))["schema"]
		if "$ref" in schema:
			inputs["body"] = (schema, bool(body.get("required")))
		for name, prop in schema.get("properties", {}).items():
			inputs[f"body:{name}"] = (prop, name in schema.get("required", []))
	return inputs


def _result(op: dict) -> dict:
	"""Properties of the ``message`` an operation answers with (200), if declared."""
	content = op.get("responses", {}).get("200", {}).get("content", {})
	schema = next(iter(content.values()), {}).get("schema", {})
	message = schema.get("properties", {}).get("message", {})
	return message.get("properties") or message.get("items", {}).get("properties") or {}


def compare(old: dict, new: dict) -> tuple[list[str], list[str]]:
	"""(breaking, other) changes from ``old`` to ``new``. Breaking: an operation or verb
	removed, guest access withdrawn, a parameter removed, retyped or newly required, a result
	property removed or retyped. Other: everything else that changes the operations, except
	their wording (summaries and descriptions)."""
	breaking, other = [], []
	old_ops = {(p, v): op for p, ops in old["paths"].items() for v, op in ops.items()}
	new_ops = {(p, v): op for p, ops in new["paths"].items() for v, op in ops.items()}
	for path, verb in sorted(old_ops.keys() - new_ops.keys()):
		breaking.append(f"{verb.upper()} {path}: removed")
	for path, verb in sorted(new_ops.keys() - old_ops.keys()):
		other.append(f"{verb.upper()} {path}: added")
	for key in sorted(old_ops.keys() & new_ops.keys()):
		before, after = old_ops[key], new_ops[key]
		where = f"{key[1].upper()} {key[0]}"
		if before.get("security") == [] and after.get("security") != []:
			breaking.append(f"{where}: no longer open to guests")
		inputs_before, inputs_after = _inputs(old, before), _inputs(new, after)
		for name, (schema, required) in inputs_before.items():
			if name not in inputs_after:
				breaking.append(f"{where}: parameter {name} removed")
			elif _plain(inputs_after[name][0]) != _plain(schema):
				breaking.append(f"{where}: parameter {name} changed type")
			elif inputs_after[name][1] and not required:
				breaking.append(f"{where}: parameter {name} now required")
		for name, (_, required) in inputs_after.items():
			if name not in inputs_before:
				(breaking if required else other).append(
					f"{where}: {'required' if required else 'optional'} parameter {name} added"
				)
		result_before, result_after = _result(before), _result(after)
		for name, schema in result_before.items():
			if name not in result_after:
				breaking.append(f"{where}: result property {name} removed")
			elif _plain(result_after[name]) != _plain(schema):
				breaking.append(f"{where}: result property {name} changed type")
		wording = ("summary", "description")
		changed = sorted(
			k
			for k in (before.keys() | after.keys()) - set(wording)
			if _plain(before.get(k), wording) != _plain(after.get(k), wording)
		)
		if changed and not any(c.startswith(f"{where}:") for c in breaking + other):
			other.append(f"{where}: {', '.join(changed)} changed")
	return breaking, other


def _version(spec: dict) -> tuple[int, ...]:
	return tuple(int(part) for part in spec["info"]["version"].split("."))


def compat(old: dict, new: dict) -> list[str]:
	"""Problems with ``new``'s version given its changes from ``old`` (D-034)."""
	breaking, other = compare(old, new)
	before, after = _version(old), _version(new)
	for change in breaking:
		print(f"breaking: {change}")
	for change in other:
		print(f"change:   {change}")
	problems = []
	if after < before:
		problems.append(f"version {new['info']['version']} is lower than {old['info']['version']}")
	elif breaking and after[0] <= before[0]:
		problems.append(
			f"breaking changes need a new major version (still {new['info']['version']}); "
			"or keep the old method and add the new behaviour under a new path (D-034)"
		)
	elif other and after <= before:
		problems.append(f"the contract changed: raise the minor version (still {new['info']['version']})")
	return problems


def main() -> int:
	if "--compat" in sys.argv:
		old = json.loads(Path(sys.argv[sys.argv.index("--compat") + 1]).read_text())
		problems = compat(old, build())
		for problem in problems:
			print(problem, file=sys.stderr)
		print(f"openapi: {len(problems)} versioning problem(s) against {old['info']['version']}")
		return 1 if problems else 0
	spec = json.dumps(build(), indent=1, sort_keys=False, ensure_ascii=False) + "\n"
	if "--check" in sys.argv:
		current = OUT.read_text() if OUT.exists() else ""
		if current != spec:
			print(f"{OUT.relative_to(ROOT)} is out of date: run scripts/dev/openapi.py", file=sys.stderr)
			return 1
		count = spec.count('"operationId"')
		print(f"openapi: {count} operations up to date")
		return 0
	OUT.write_text(spec)
	print(f"openapi: wrote {OUT.relative_to(ROOT)}")
	return 0


if __name__ == "__main__":
	sys.exit(main())
