"""Approval engine: policy -> request -> immutable decisions (ERP-05, ERP-09, ERP-10).

* An **Approval Policy** says which documents of a DocType need approval (company,
  amount range, category) and lists ordered steps by role, optionally only above an
  amount.
* ``request_approval`` opens an **Approval Request**, binding it to a SHA-256 of the
  document fields the policy protects.
* ``decide`` records an immutable **Approval Decision** for the current step after
  checking authority (role or an active **Approval Delegation**), maker/checker and
  distinct-approver rules, and that the document is unchanged.
* ``enforce_on_submit`` (wildcard before_submit hook) refuses to submit a governed
  document without an Approved request whose hash matches the document.

Approval Requests are written only through this module (controller guard).
"""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, getdate, now_datetime, nowdate

from automotive_core.governance import audit
from automotive_core.utils import canonical_json, sha256_hex

GOVERNED_CACHE_KEY = "automotive_governed_doctypes"
HEADER_HASH_FIELDS = (
	"company",
	"supplier",
	"customer",
	"party_type",
	"party",
	"posting_date",
	"transaction_date",
	"schedule_date",
	"delivery_date",
	"currency",
	"grand_total",
	"base_grand_total",
	"paid_amount",
	"received_amount",
	"discount_amount",
	"additional_discount_percentage",
	"total_debit",
	"total_credit",
	"party_bank_account",
)
ROW_HASH_FIELDS = (
	"item_code",
	"qty",
	"rate",
	"amount",
	"uom",
	"warehouse",
	"discount_percentage",
	"account",
	"debit",
	"credit",
	"reference_doctype",
	"reference_name",
	"allocated_amount",
)


class ApprovalRequiredError(frappe.ValidationError):
	pass


class ApprovalAuthorityError(frappe.PermissionError):
	pass


# ---------------------------------------------------------------------------------------
# Policy resolution
# ---------------------------------------------------------------------------------------


def governed_doctypes() -> frozenset[str]:
	def load() -> list[str]:
		return frappe.get_all("Approval Policy", filters={"is_active": 1}, pluck="reference_doctype", distinct=True)

	return frozenset(frappe.cache.get_value(GOVERNED_CACHE_KEY, generator=load) or [])


def clear_policy_cache() -> None:
	frappe.cache.delete_value(GOVERNED_CACHE_KEY)


def document_amount(doc, amount_field: str | None) -> float:
	return flt(doc.get(amount_field)) if amount_field else 0.0


def _field_values(doc, path: str) -> set[str]:
	"""'item_group' -> {doc.item_group}; 'items.item_group' -> values of every items row."""
	if "." in path:
		table, field = path.split(".", 1)
		return {str(row.get(field)) for row in doc.get(table) or [] if row.get(field)}
	value = doc.get(path)
	return {str(value)} if value not in (None, "") else set()


def find_policy(doc) -> Any | None:
	policies = frappe.get_all(
		"Approval Policy",
		filters={"reference_doctype": doc.doctype, "is_active": 1},
		fields=["name", "applies_to_company", "amount_field", "min_amount", "max_amount", "category_field",
			"category_values"],
		order_by="priority desc, name asc",
	)
	for p in policies:
		if p.applies_to_company and p.applies_to_company != doc.get("company"):
			continue
		amount = document_amount(doc, p.amount_field)
		if p.min_amount and amount < flt(p.min_amount):
			continue
		if p.max_amount and amount > flt(p.max_amount):
			continue
		if p.category_field and p.category_values:
			wanted = {v.strip() for v in p.category_values.splitlines() if v.strip()}
			if not wanted & _field_values(doc, p.category_field):
				continue
		return frappe.get_cached_doc("Approval Policy", p.name)
	return None


def _normalise(value: Any) -> Any:
	if isinstance(value, bool) or value is None:
		return value
	if isinstance(value, int | float | decimal.Decimal):
		return f"{flt(value, 6):.6f}"
	if isinstance(value, dt.datetime | dt.date | dt.time | dt.timedelta):
		return str(value)
	return str(value)


def document_hash(doc, policy) -> str:
	"""SHA-256 over the fields an approval binds (policy.hash_fields or a sensible default)."""
	explicit = [f.strip() for f in (policy.hash_fields or "").split(",") if f.strip()]
	material: dict[str, Any] = {"doctype": doc.doctype, "name": doc.name}
	if explicit:
		for field in explicit:
			if "." in field:
				table, column = field.split(".", 1)
				material[field] = [_normalise(row.get(column)) for row in doc.get(table) or []]
			else:
				material[field] = _normalise(doc.get(field))
	else:
		fields = [f for f in (policy.amount_field, *HEADER_HASH_FIELDS) if f and doc.meta.has_field(f)]
		for field in dict.fromkeys(fields):
			material[field] = _normalise(doc.get(field))
		for table in ("items", "accounts", "references", "taxes"):
			if doc.meta.has_field(table):
				material[table] = [
					{f: _normalise(row.get(f)) for f in ROW_HASH_FIELDS if row.meta.has_field(f)}
					for row in doc.get(table) or []
				]
	return sha256_hex(canonical_json(material))


# ---------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------


def latest_request(doctype: str, name: str, statuses: tuple[str, ...] | None = None):
	filters: dict[str, Any] = {"reference_doctype": doctype, "reference_name": name}
	if statuses:
		filters["status"] = ("in", statuses)
	found = frappe.get_all("Approval Request", filters=filters, order_by="creation desc", limit=1, pluck="name")
	return frappe.get_doc("Approval Request", found[0]) if found else None


def _save_request(req) -> None:
	req.flags.ignore_permissions = True
	req.flags.automotive_approval_service = True
	if req.is_new():
		req.insert()
	else:
		req.save()


@frappe.whitelist(methods=["POST"])
def request_approval(doctype: str, name: str, remarks: str | None = None) -> dict:
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("write")
	if doc.docstatus != 0:
		frappe.throw(_("Only draft documents can be sent for approval."))
	policy = find_policy(doc)
	if not policy:
		frappe.throw(_("No active approval policy applies to {0} {1}.").format(_(doctype), name))

	current_hash = document_hash(doc, policy)
	existing = latest_request(doctype, name, ("Pending", "Approved"))
	if existing and existing.document_hash == current_hash and existing.policy == policy.name:
		return _request_summary(existing)
	if existing:
		_invalidate(existing, _("Superseded by a new request after the document changed."))

	amount = document_amount(doc, policy.amount_field)
	steps = [s for s in sorted(policy.steps, key=lambda s: s.step_no) if not s.min_amount or amount >= flt(s.min_amount)]
	if not steps:
		frappe.throw(_("Policy {0} has no step for an amount of {1}.").format(policy.name, amount))

	req = frappe.new_doc("Approval Request")
	req.update(
		{
			"reference_doctype": doctype,
			"reference_name": name,
			"company": doc.get("company"),
			"policy": policy.name,
			"status": "Pending",
			"amount": amount,
			"currency": _currency(doc),
			"current_step": steps[0].step_no,
			"requested_by": frappe.session.user,
			"requested_at": now_datetime(),
			"document_hash": current_hash,
			"remarks": remarks,
		}
	)
	for s in steps:
		req.append(
			"steps",
			{
				"step_no": s.step_no,
				"step_name": s.step_name,
				"approver_role": s.approver_role,
				"approver_user": s.approver_user,
				"escalate_after_hours": s.escalate_after_hours,
				"status": "Pending",
			},
		)
	_save_request(req)
	audit.record(
		"approval.requested",
		doctype,
		name,
		{"request": req.name, "policy": policy.name, "amount": amount, "document_hash": current_hash},
		company=doc.get("company"),
	)
	_notify_step(req)
	return _request_summary(req)


def _currency(doc) -> str | None:
	if doc.meta.has_field("currency") and doc.get("currency"):
		return doc.currency
	if doc.get("company"):
		return frappe.get_cached_value("Company", doc.company, "default_currency")
	return None


def _request_summary(req) -> dict:
	return {
		"name": req.name,
		"status": req.status,
		"current_step": req.current_step,
		"steps": [
			{"step_no": s.step_no, "step_name": s.step_name, "role": s.approver_role, "status": s.status,
			 "decided_by": s.decided_by, "on_behalf_of": s.on_behalf_of}
			for s in req.steps
		],
	}


def _invalidate(req, reason: str) -> None:
	req.status = "Invalidated"
	req.completed_at = now_datetime()
	req.remarks = reason
	_save_request(req)
	audit.record("approval.invalidated", req.reference_doctype, req.reference_name, {"request": req.name, "reason": reason})


@frappe.whitelist(methods=["POST"])
def withdraw(request: str, reason: str | None = None) -> dict:
	req = frappe.get_doc("Approval Request", request, for_update=True)
	if req.status != "Pending":
		frappe.throw(_("Only pending requests can be withdrawn."))
	if frappe.session.user not in (req.requested_by, "Administrator") and "System Manager" not in frappe.get_roles():
		raise frappe.PermissionError(_("Only the requester can withdraw an approval request."))
	req.status = "Withdrawn"
	req.completed_at = now_datetime()
	req.remarks = reason
	_save_request(req)
	audit.record("approval.withdrawn", req.reference_doctype, req.reference_name, {"request": req.name, "reason": reason})
	return _request_summary(req)


# ---------------------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------------------


def _current_step(req):
	for step in sorted(req.steps, key=lambda s: s.step_no):
		if step.status == "Pending":
			return step
	return None


def resolve_authority(user: str, step, req, doc) -> tuple[str | None, str | None]:
	"""Return (on_behalf_of, delegation) if ``user`` may decide ``step``; raise otherwise."""
	from automotive_core.governance.delegation import find_active_delegation

	policy = frappe.get_cached_doc("Approval Policy", req.policy)
	if step.approver_user:
		if user == step.approver_user:
			return None, None
	elif step.approver_role in frappe.get_roles(user):
		return None, None
	if policy.allow_delegation:
		delegation = find_active_delegation(
			delegate=user,
			role=step.approver_role,
			delegator=step.approver_user,
			company=req.company,
			doctype=req.reference_doctype,
			amount=flt(req.amount),
		)
		if delegation:
			return delegation.delegator, delegation.name
	raise ApprovalAuthorityError(
		_("You are not authorised to approve step {0} ({1}) of {2}.").format(step.step_no, step.approver_role, req.name)
	)


def _check_separation(user: str, on_behalf_of: str | None, req, doc, policy) -> None:
	makers = {doc.owner, req.requested_by}
	if policy.maker_cannot_approve:
		for actor in filter(None, (user, on_behalf_of)):
			if actor in makers:
				raise ApprovalAuthorityError(_("The maker of {0} cannot approve it (maker/checker).").format(doc.name))
	if policy.distinct_approvers:
		previous = {s.decided_by for s in req.steps if s.decided_by} | {s.on_behalf_of for s in req.steps if s.on_behalf_of}
		for actor in filter(None, (user, on_behalf_of)):
			if actor in previous:
				raise ApprovalAuthorityError(_("Each step of {0} must be approved by a different person.").format(req.name))


@frappe.whitelist(methods=["POST"])
def decide(request: str, decision: str, comment: str | None = None) -> dict:
	if decision not in ("Approve", "Reject"):
		frappe.throw(_("Decision must be Approve or Reject."))
	user = frappe.session.user
	req = frappe.get_doc("Approval Request", request, for_update=True)
	if req.status != "Pending":
		frappe.throw(_("Approval Request {0} is {1}, not Pending.").format(req.name, _(req.status)))
	step = _current_step(req)
	if not step:
		frappe.throw(_("Approval Request {0} has no pending step.").format(req.name))
	if decision == "Reject" and not (comment or "").strip():
		frappe.throw(_("A comment is required when rejecting."))

	doc = frappe.get_doc(req.reference_doctype, req.reference_name)
	policy = frappe.get_cached_doc("Approval Policy", req.policy)
	if doc.docstatus != 0:
		frappe.throw(_("{0} is no longer a draft.").format(doc.name))
	on_behalf_of, delegation = resolve_authority(user, step, req, doc)
	_check_separation(user, on_behalf_of, req, doc, policy)

	current_hash = document_hash(doc, policy)
	if policy.bind_to_document_hash and current_hash != req.document_hash:
		_invalidate(req, _("The document changed after approval was requested."))
		return _request_summary(req) | {"message": _("The document changed; a new approval request is needed.")}

	decided_at = now_datetime()
	decision_doc = frappe.get_doc(
		{
			"doctype": "Approval Decision",
			"approval_request": req.name,
			"reference_doctype": req.reference_doctype,
			"reference_name": req.reference_name,
			"step_no": step.step_no,
			"decision": decision,
			"decided_by": user,
			"on_behalf_of": on_behalf_of,
			"delegation": delegation,
			"decided_at": decided_at,
			"document_hash": current_hash,
			"client_ip": getattr(frappe.local, "request_ip", None),
			"comment": comment,
		}
	)
	decision_doc.flags.ignore_permissions = True
	decision_doc.flags.automotive_approval_service = True
	decision_doc.insert()

	step.status = "Approved" if decision == "Approve" else "Rejected"
	step.decided_by = user
	step.on_behalf_of = on_behalf_of
	step.decided_at = decided_at
	step.decision = decision_doc.name
	if decision == "Reject":
		req.status = "Rejected"
		req.completed_at = decided_at
	else:
		nxt = _current_step(req)
		if nxt:
			req.current_step = nxt.step_no
		else:
			req.status = "Approved"
			req.completed_at = decided_at
	_save_request(req)
	audit.record(
		f"approval.{decision.lower()}",
		req.reference_doctype,
		req.reference_name,
		{"request": req.name, "step": step.step_no, "decision": decision_doc.name, "delegation": delegation},
		company=req.company,
		on_behalf_of=on_behalf_of,
	)
	if req.status == "Pending":
		_notify_step(req)
	else:
		_notify_requester(req)
	return _request_summary(req)


# ---------------------------------------------------------------------------------------
# Enforcement and queries
# ---------------------------------------------------------------------------------------


def enforce_on_submit(doc, method=None) -> None:
	if doc.doctype not in governed_doctypes():
		return
	policy = find_policy(doc)
	if not policy:
		return
	req = latest_request(doc.doctype, doc.name, ("Approved",))
	if not req or req.policy != policy.name:
		raise ApprovalRequiredError(
			_("{0} {1} needs approval under policy {2} before it can be submitted.").format(
				_(doc.doctype), doc.name, policy.name
			)
		)
	if policy.bind_to_document_hash and document_hash(doc, policy) != req.document_hash:
		raise ApprovalRequiredError(
			_("{0} {1} changed after it was approved ({2}); request approval again.").format(
				_(doc.doctype), doc.name, req.name
			)
		)
	doc.flags.automotive_approval_request = req.name


def actionable_for(user: str | None = None) -> list[str]:
	"""Names of pending requests whose current step ``user`` may decide (role or delegation)."""
	from automotive_core.governance.delegation import delegated_roles

	user = user or frappe.session.user
	roles = set(frappe.get_roles(user))
	delegated = delegated_roles(user)
	# The maker/checker and distinct-approver rules need the document itself and are
	# enforced in decide(); the inbox only hides the requester's own requests.
	rows = frappe.db.sql(
		"""select r.name, r.company, r.reference_doctype, r.amount, r.requested_by, s.approver_role,
			s.approver_user
		from `tabApproval Request` r
		join `tabApproval Request Step` s on s.parent = r.name and s.step_no = r.current_step
		where r.status = 'Pending' and s.status = 'Pending'
		order by r.requested_at asc""",
		as_dict=True,
	)
	result = []
	for row in rows:
		if row.requested_by == user:
			continue
		if row.approver_user:
			if row.approver_user == user or any(
				d.delegator == row.approver_user and d.role == row.approver_role and d.applies(row) for d in delegated
			):
				result.append(row.name)
			continue
		if row.approver_role in roles or any(d.role == row.approver_role and d.applies(row) for d in delegated):
			result.append(row.name)
	return result


@frappe.whitelist()
def get_inbox() -> list[dict]:
	names = actionable_for()
	if not names:
		return []
	return frappe.get_all(
		"Approval Request",
		filters={"name": ("in", names)},
		fields=["name", "reference_doctype", "reference_name", "company", "amount", "currency", "current_step",
			"requested_by", "requested_at"],
		order_by="requested_at asc",
	)


@frappe.whitelist()
def get_status(doctype: str, name: str) -> dict:
	doc = frappe.get_doc(doctype, name)
	doc.check_permission("read")
	policy = find_policy(doc) if doc.docstatus == 0 else None
	req = latest_request(doctype, name)
	summary: dict[str, Any] = {"policy": policy.name if policy else None, "request": None, "can_decide": False}
	if req:
		summary["request"] = _request_summary(req)
		summary["hash_matches"] = bool(policy) and document_hash(doc, policy) == req.document_hash
		summary["can_decide"] = req.status == "Pending" and req.name in actionable_for()
	return summary


def _notify_step(req) -> None:
	from automotive_core.governance.notifications import notify

	step = _current_step(req)
	if not step:
		return
	users = [step.approver_user] if step.approver_user else _users_with_role(step.approver_role, req.company)
	notify("approval.requested", users, "Approval Request", req.name, step_no=step.step_no, step_name=step.step_name,
		requester=frappe.utils.get_fullname(req.requested_by))


def _notify_requester(req) -> None:
	from automotive_core.governance.notifications import notify

	notify("approval.decided", [req.requested_by], "Approval Request", req.name)


# after the last step, an overdue approval goes to group finance control
ESCALATION_ROLE = "Group Controller"


def _step_started(req, step):
	"""When a step became current: the request, or the decision of the step before it."""
	earlier = [s.decided_at for s in req.steps if s.step_no < step.step_no and s.decided_at]
	return max(earlier) if earlier else req.requested_at


def escalate_overdue(now=None) -> list[str]:
	"""Hourly: a pending step waiting longer than its escalation time (copied from the policy
	when the approval was requested) is escalated once. Its approvers are reminded, and the
	next level is alerted: the next step's approvers, or the group controllers after the last
	step. The step records when and to whom; the audit chain records the escalation."""
	from automotive_core.governance.notifications import notify

	now = get_datetime(now or now_datetime())
	escalated = []
	for name in frappe.get_all("Approval Request", filters={"status": "Pending"}, pluck="name", order_by="requested_at"):
		req = frappe.get_doc("Approval Request", name)
		step = _current_step(req)
		if not step or step.escalated_at or cint(step.escalate_after_hours) <= 0:
			continue
		waited = (now - get_datetime(_step_started(req, step))).total_seconds() / 3600
		if waited < cint(step.escalate_after_hours):
			continue
		later = [s for s in sorted(req.steps, key=lambda s: s.step_no) if s.step_no > step.step_no and s.status == "Pending"]
		if later:
			above = [later[0].approver_user] if later[0].approver_user else _users_with_role(later[0].approver_role, req.company)
		else:
			above = _users_with_role(ESCALATION_ROLE, req.company)
		approvers = [step.approver_user] if step.approver_user else _users_with_role(step.approver_role, req.company)
		frappe.db.set_value("Approval Request Step", step.name, {"escalated_at": now,
			"escalated_to": "\n".join(above)}, update_modified=False)
		notify("approval.escalated", sorted(set(approvers) | set(above)), "Approval Request", req.name,
			step_no=step.step_no, step_name=step.step_name, hours=int(waited))
		audit.record("approval.escalated", req.reference_doctype, req.reference_name,
			{"request": req.name, "step": step.step_no, "hours": int(waited), "escalated_to": above}, company=req.company)
		escalated.append(req.name)
	return escalated


def _users_with_role(role: str, company: str | None) -> list[str]:
	users = frappe.get_all(
		"Has Role", filters={"role": role, "parenttype": "User"}, pluck="parent", distinct=True
	)
	enabled = set(frappe.get_all("User", filters={"name": ("in", users), "enabled": 1}, pluck="name")) if users else set()
	return sorted(u for u in enabled if u not in ("Administrator", "Guest"))[:50]


def is_delegation_valid_on(delegation, on_date: str | None = None) -> bool:
	day = getdate(on_date or nowdate())
	return delegation.docstatus == 1 and getdate(delegation.from_date) <= day <= getdate(delegation.to_date)
