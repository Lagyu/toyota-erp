"""Account lifecycle: provisioning, change, deprovisioning and expiry (ERP-10).

A User Provisioning Request is a submittable document; approval is enforced by the
generic approval engine when an Approval Policy exists for it (the tenant baseline
creates one). Submitting applies the change atomically and writes an audit event.
Deactivation disables the account, ends its sessions and removes API credentials.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate, nowdate, validate_email_address

from automotive_core.access import user_permissions
from automotive_core.governance import audit
from automotive_core.governance.sod import SegregationOfDutiesError, conflicts
from automotive_core.utils import commit, send_welcome_email

MANAGED_BY = "User Provisioning Request"


def desired_roles(doc) -> set[str]:
	roles: set[str] = set()
	if doc.role_profile:
		roles |= {r.role for r in frappe.get_cached_doc("Role Profile", doc.role_profile).roles}
	roles |= {r.role for r in doc.get("extra_roles") or []}
	return roles


def validate(doc) -> None:
	doc.email = (doc.email or "").strip().lower()
	validate_email_address(doc.email, throw=True)
	if doc.request_type == "Create":
		if frappe.db.exists("User", doc.email):
			frappe.throw(_("User {0} already exists; use a Modify request.").format(doc.email))
		if not doc.first_name:
			frappe.throw(_("First Name is required to create an account."))
	else:
		if not doc.user or not frappe.db.exists("User", doc.user):
			frappe.throw(_("Select the existing user."))
		if doc.user in ("Administrator", "Guest"):
			frappe.throw(_("Standard users cannot be provisioned."))
		doc.email = doc.user
	if doc.request_type in ("Create", "Modify"):
		blocking = [r for r in conflicts(desired_roles(doc)) if r.enforcement == "Block"]
		if blocking:
			raise SegregationOfDutiesError(
				_("The requested roles violate segregation of duties: {0}").format(
					", ".join(f"{r.role_a} + {r.role_b}" for r in blocking))
			)
	for scope in doc.get("scopes") or []:
		if not frappe.db.exists(scope.allow, scope.for_value):
			frappe.throw(_("Row {0}: {1} {2} does not exist.").format(scope.idx, scope.allow, scope.for_value))
	if doc.access_expiry_date and getdate(doc.access_expiry_date) < getdate(nowdate()):
		frappe.throw(_("Access expiry cannot be in the past."))
	if doc.docstatus == 0:
		doc.status = "Pending Approval" if doc.flags.get("in_approval") else (doc.status or "Draft")


def on_submit(doc) -> None:
	result = apply(doc)
	doc.db_set({"status": "Applied", "result": result})
	audit.record(
		f"user.{doc.request_type.lower()}",
		"User",
		doc.email,
		{"request": doc.name, "role_profile": doc.role_profile, "roles": sorted(desired_roles(doc)),
		 "scopes": [(s.allow, s.for_value) for s in doc.get("scopes") or []],
		 "expires": str(doc.access_expiry_date or "")},
	)


def apply(doc) -> str:
	if doc.request_type == "Create":
		user = frappe.new_doc("User")
		user.update(
			{
				"email": doc.email,
				"first_name": doc.first_name,
				"last_name": doc.last_name,
				"user_type": doc.user_type,
				"language": doc.language or "en",
				"send_welcome_email": send_welcome_email(),
				"automotive_access_expiry_date": doc.access_expiry_date,
			}
		)
		_set_roles(user, doc)
		user.flags.ignore_permissions = True
		user.insert()
		_apply_scopes(user.name, doc)
		return _("Created {0} with {1} role(s).").format(user.name, len(user.roles))

	user = frappe.get_doc("User", doc.user)
	user.flags.ignore_permissions = True
	if doc.request_type == "Modify":
		_set_roles(user, doc)
		if doc.access_expiry_date:
			user.automotive_access_expiry_date = doc.access_expiry_date
		user.save()
		_apply_scopes(user.name, doc)
		return _("Updated {0}: {1} role(s).").format(user.name, len(user.roles))
	if doc.request_type == "Deactivate":
		deactivate(user.name, reason=doc.justification)
		return _("Deactivated {0}; sessions and API keys revoked.").format(user.name)
	user.enabled = 1
	user.save()
	return _("Reactivated {0}.").format(user.name)


def _set_roles(user, doc) -> None:
	extra = [r.role for r in doc.get("extra_roles") or []]
	if doc.role_profile and not extra:
		# role_profiles replaces the role list on save (frappe.core.doctype.user.user)
		user.set("role_profiles", [{"role_profile": doc.role_profile}])
		return
	user.set("role_profiles", [])
	user.set("roles", [])
	user.append_roles(*sorted(desired_roles(doc)))


def _apply_scopes(user: str, doc) -> None:
	desired = {(s.allow, s.for_value): bool(s.is_default) for s in doc.get("scopes") or []}
	user_permissions.sync(user, MANAGED_BY, desired)


def deactivate(user: str, reason: str | None = None) -> None:
	from frappe.sessions import clear_sessions
	from frappe.utils.password import remove_encrypted_password

	frappe.db.set_value("User", user, "enabled", 0)
	clear_sessions(user=user, force=True)
	frappe.db.set_value("User", user, "api_key", None, update_modified=False)
	remove_encrypted_password("User", user, "api_secret")
	audit.record("user.deactivated", "User", user, {"reason": reason})


def expire_accounts() -> None:
	"""Daily: disable accounts whose access expiry date has passed."""
	today = getdate(nowdate())
	users = frappe.get_all(
		"User",
		filters={"enabled": 1, "automotive_access_expiry_date": ("<", today)},
		pluck="name",
	)
	for user in users:
		if user in ("Administrator", "Guest"):
			continue
		deactivate(user, reason="access expiry date passed")
	commit()
