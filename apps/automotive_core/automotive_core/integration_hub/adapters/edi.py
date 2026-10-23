"""Hinode EDI: a segment-based, X12-inspired interchange format for partners that
exchange flat files (ERP-11). It is a project-defined profile, not an ANSI X12 or
EDIFACT implementation; mapping to a real standard belongs in a partner-specific
adapter once a partner's implementation guide is available.

Structure (segments end with ``~``, elements are separated by ``*``)::

    ISA*HINODE*1*<interchange control>~
    ST*<message or event type>*<message id>~
    BGN*<schema version>*<sent/occurred at ISO-8601>~
    DTA*<json path>*<type: S|N|B|Z>*<value>~      one per leaf value of "data"
    SE*<segment count incl. ST and SE>*<message id>~
    IEA*1*<interchange control>~

Reserved characters ``* ~ ?`` inside values are escaped with ``?`` (release char).
Round trip: decode(encode(x)) == x for JSON objects whose leaves are str, int, float,
bool or null (lists become index paths).
"""

from __future__ import annotations

import hashlib
from typing import Any

RELEASE = "?"
SEG = "~"
ELEM = "*"


class EdiError(ValueError):
	pass


def _escape(value: str) -> str:
	return value.replace(RELEASE, RELEASE * 2).replace(ELEM, RELEASE + ELEM).replace(SEG, RELEASE + SEG)


def _split(text: str, sep: str) -> list[str]:
	out, buf, i = [], [], 0
	while i < len(text):
		ch = text[i]
		if ch == RELEASE and i + 1 < len(text):
			buf.append(ch + text[i + 1])  # keep escaped pair; unescaped later
			i += 2
			continue
		if ch == sep:
			out.append("".join(buf))
			buf = []
		else:
			buf.append(ch)
		i += 1
	out.append("".join(buf))
	return out


def _unescape(value: str) -> str:
	out, i = [], 0
	while i < len(value):
		if value[i] == RELEASE and i + 1 < len(value):
			out.append(value[i + 1])
			i += 2
		else:
			out.append(value[i])
			i += 1
	return "".join(out)


def _flatten(prefix: str, value: Any, out: list[tuple[str, str, str]]) -> None:
	if isinstance(value, dict):
		if not value:
			out.append((prefix, "O", ""))
		for key in sorted(value):
			if "." in str(key):
				raise EdiError(f"keys may not contain '.': {key!r}")
			_flatten(f"{prefix}.{key}" if prefix else str(key), value[key], out)
	elif isinstance(value, list):
		if not value:
			out.append((prefix, "L", ""))
		for i, item in enumerate(value):
			_flatten(f"{prefix}.{i}", item, out)
	elif value is None:
		out.append((prefix, "Z", ""))
	elif isinstance(value, bool):
		out.append((prefix, "B", "1" if value else "0"))
	elif isinstance(value, int | float):
		out.append((prefix, "N", repr(value)))
	else:
		out.append((prefix, "S", str(value)))


def encode(envelope: dict[str, Any]) -> str:
	message_id = str(envelope.get("message_id") or envelope.get("event_id") or "")
	message_type = str(envelope.get("message_type") or envelope.get("event_type") or "")
	sent = str(envelope.get("sent_at") or envelope.get("occurred_at") or "")
	control = hashlib.sha256(message_id.encode()).hexdigest()[:9]
	leaves: list[tuple[str, str, str]] = []
	_flatten("", envelope.get("data") or {}, leaves)
	segments = [
		["ISA", "HINODE", "1", control],
		["ST", message_type, message_id],
		["BGN", str(envelope.get("schema_version") or "v1"), sent],
		*[["DTA", path, kind, value] for path, kind, value in leaves],
	]
	segments.append(["SE", str(len(segments)), message_id])  # ST..last DTA + SE itself
	segments.append(["IEA", "1", control])
	return "".join(ELEM.join(_escape(e) for e in seg) + SEG + "\n" for seg in segments)


def _set_path(root: dict, path: str, value: Any) -> None:
	keys = path.split(".")
	node: Any = root
	for i, key in enumerate(keys):
		last = i == len(keys) - 1
		nxt_is_index = not last and keys[i + 1].isdigit()
		if isinstance(node, list):
			index = int(key)
			while len(node) <= index:
				node.append(None)
			if last:
				node[index] = value
			else:
				if node[index] is None:
					node[index] = [] if nxt_is_index else {}
				node = node[index]
		else:
			if last:
				node[key] = value
			else:
				node = node.setdefault(key, [] if nxt_is_index else {})


def decode(text: str) -> dict[str, Any]:
	segments = [s.strip("\r\n") for s in _split(text, SEG)]
	segments = [s for s in segments if s.strip()]
	parsed = [[_unescape(e) for e in _split(s, ELEM)] for s in segments]
	if not parsed or parsed[0][0] != "ISA" or parsed[-1][0] != "IEA":
		raise EdiError("missing ISA/IEA envelope")
	body = parsed[1:-1]
	if not body or body[0][0] != "ST" or body[-1][0] != "SE":
		raise EdiError("missing ST/SE transaction set")
	if int(body[-1][1]) != len(body):
		raise EdiError(f"segment count mismatch: SE says {body[-1][1]}, found {len(body)}")
	_, message_type, message_id = body[0][:3]
	if body[-1][2] != message_id:
		raise EdiError("SE control number does not match ST")
	if parsed[0][3] != parsed[-1][2]:
		raise EdiError("ISA/IEA control numbers differ")
	envelope: dict[str, Any] = {"message_id": message_id, "message_type": message_type, "data": {}}
	for seg in body[1:-1]:
		tag = seg[0]
		if tag == "BGN":
			envelope["schema_version"] = seg[1]
			if len(seg) > 2 and seg[2]:
				envelope["sent_at"] = seg[2]
		elif tag == "DTA":
			if len(seg) != 4:
				raise EdiError(f"bad DTA segment: {seg}")
			path, kind, raw = seg[1], seg[2], seg[3]
			value: Any
			if kind == "S":
				value = raw
			elif kind == "N":
				value = float(raw) if any(c in raw for c in ".eE") else int(raw)
			elif kind == "B":
				value = raw == "1"
			elif kind == "Z":
				value = None
			elif kind == "O":
				value = {}
			elif kind == "L":
				value = []
			else:
				raise EdiError(f"unknown value type {kind!r}")
			if path:
				_set_path(envelope["data"], path, value)
		else:
			raise EdiError(f"unexpected segment {tag}")
	return envelope
