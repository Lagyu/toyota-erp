"""Database-level guards for append-only records.

* Composite unique keys that Frappe JSON cannot declare (inbound message dedup).
* Triggers that make sealed Audit Events and every Approval Decision immutable even
  for application code that bypasses the ORM. The hash-chain verifier remains the
  detection control if a DBA disables the triggers.

Trigger creation needs the TRIGGER privilege (Frappe's site user has ALL on its
database). With binary logging enabled MariaDB additionally requires
log_bin_trust_function_creators=1; see ops/runbooks/database.md. Failure to create a
trigger is logged and reported by the health check instead of failing the migrate.
"""

from __future__ import annotations

import frappe

UNIQUE_KEYS = [
	("Integration Inbound Message", ("partner", "message_id"), "uniq_partner_message"),
]

TRIGGERS = {
	"automotive_audit_event_no_update": """
		CREATE TRIGGER `automotive_audit_event_no_update` BEFORE UPDATE ON `tabAudit Event`
		FOR EACH ROW BEGIN
			IF OLD.event_hash IS NOT NULL AND OLD.event_hash <> '' THEN
				SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Audit Event is sealed and immutable';
			END IF;
			IF NOT (NEW.event_type <=> OLD.event_type AND NEW.reference_doctype <=> OLD.reference_doctype
				AND NEW.reference_name <=> OLD.reference_name AND NEW.actor <=> OLD.actor
				AND NEW.event_time <=> OLD.event_time AND NEW.payload <=> OLD.payload) THEN
				SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Audit Event content is immutable';
			END IF;
		END""",
	"automotive_audit_event_no_delete": """
		CREATE TRIGGER `automotive_audit_event_no_delete` BEFORE DELETE ON `tabAudit Event`
		FOR EACH ROW BEGIN
			IF @automotive_allow_audit_purge IS NULL OR @automotive_allow_audit_purge <> 1 THEN
				SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Audit Events cannot be deleted';
			END IF;
		END""",
	"automotive_approval_decision_no_update": """
		CREATE TRIGGER `automotive_approval_decision_no_update` BEFORE UPDATE ON `tabApproval Decision`
		FOR EACH ROW BEGIN
			SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Approval Decisions are immutable';
		END""",
	"automotive_approval_decision_no_delete": """
		CREATE TRIGGER `automotive_approval_decision_no_delete` BEFORE DELETE ON `tabApproval Decision`
		FOR EACH ROW BEGIN
			IF @automotive_allow_audit_purge IS NULL OR @automotive_allow_audit_purge <> 1 THEN
				SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Approval Decisions cannot be deleted';
			END IF;
		END""",
}


def apply() -> None:
	for doctype, columns, index_name in UNIQUE_KEYS:
		_ensure_unique(doctype, columns, index_name)
	existing = {row[0] for row in frappe.db.sql("SHOW TRIGGERS")}
	for name, ddl in TRIGGERS.items():
		if name in existing:
			continue
		try:
			frappe.db.sql(ddl)
		except Exception as exc:
			frappe.log_error(title=f"automotive_core: could not create trigger {name}", message=str(exc))


def remove() -> None:
	for name in TRIGGERS:
		frappe.db.sql(f"DROP TRIGGER IF EXISTS `{name}`")


def missing_triggers() -> list[str]:
	existing = {row[0] for row in frappe.db.sql("SHOW TRIGGERS")}
	return [name for name in TRIGGERS if name not in existing]


def _ensure_unique(doctype: str, columns: tuple[str, ...], index_name: str) -> None:
	table = f"tab{doctype}"
	present = frappe.db.sql(f"SHOW INDEX FROM `{table}` WHERE Key_name = %s", index_name)
	if not present:
		cols = ", ".join(f"`{c}`" for c in columns)
		frappe.db.sql_ddl(f"ALTER TABLE `{table}` ADD UNIQUE INDEX `{index_name}` ({cols})")
