# Permission matrix

Desk rights of each internal persona (its role profile in
`apps/automotive_core/automotive_core/setup/roles.py`) on the key DocTypes, at permission
level 0 (GATE-4). Rights: **r** read, **w** write, **c** create, **s** submit, **x** cancel;
**-** none; after a slash, rights on the user's own documents only (`rc/w`: reads and creates,
and edits what they created). They come from the standard ERPNext/Frappe permissions of the
persona's roles plus the automotive grants (`automotive_core.setup.permissions`).

Row-level scope narrows these rights further: company and plant User Permissions from
provisioning and plant assignments (`automotive_core.access`), partner scope in the portals,
and the approval service's maker/checker and delegation rules. Restricted fields (supplier
bank details, an employee's personal details) sit at permission level 1:
[restricted fields and exports](#restricted-fields-and-exports-sec-4).

Actions that change a document past these rights (acknowledging an andon, releasing a hold,
completing an operation, releasing a planning run, building a plant schedule, accepting a
dealer order) check the role they need first, then load the document with
`plant_scope.get_scoped_doc`, which applies the caller's User Permissions: they act only on
documents of their own plant and company ([findings](findings.md), C-10).

`automotive_core.tests.test_permission_matrix` checks the live permission tables against
this table: change both together.

| DocType | AP Clerk | Auditor | Automotive Executive | Buyer | Dealer Liaison | Engineering Manager | Group Controller | IT Operator | Line Operator | Logistics Coordinator | Plant Director | Procurement Manager | Product Engineer | Production Planner | Production Supervisor | Quality Inspector | Quality Manager | Sales Manager | Treasury Approver | Warehouse Clerk |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Purchase Order | r | r | r | rwcsx | - | - | - | - | - | r | r | rwcsx | - | r | - | - | - | - | - | r |
| Purchase Receipt | r | r | - | rwcsx | - | - | - | - | - | rwcsx | rwcsx | - | - | rwcsx | - | r | - | - | - | rwcsx |
| Purchase Invoice | rwcsx | r | r | r | - | - | rwcsx | - | - | - | - | - | - | - | - | - | - | - | r | - |
| Payment Entry | rwcsx | r | - | - | - | - | rwcsx | - | - | - | - | - | - | - | - | - | - | - | r | - |
| Journal Entry | rwcsx | r | - | - | - | - | rwcsx | - | - | - | - | - | - | - | - | - | - | - | - | - |
| Sales Order | r | r | r | - | rwcsx | - | - | - | - | r | r | - | - | r | - | - | - | rwcsx | - | r |
| Sales Invoice | rwcs | r | r | - | - | - | rwcsx | - | - | - | - | - | - | - | - | - | - | - | - | - |
| Delivery Note | r | r | - | - | rwcsx | - | - | - | - | rwcsx | rwcsx | - | - | rwcsx | - | r | - | rwcsx | - | rwcsx |
| Stock Entry | - | r | r | - | - | rwcsx | - | - | - | rwcsx | rwcsx | - | - | rwcsx | rwcsx | r | - | - | - | rwcsx |
| Material Request | - | r | - | rwcsx | - | r | - | - | - | rwcsx | rwcsx | rwcsx | - | rwcsx | - | - | - | - | - | rwcsx |
| Pick List | - | r | - | - | - | rwcsx | - | - | - | rwcsx | rwcsx | - | - | rwcsx | rwcsx | - | - | - | - | rwcsx |
| Work Order | - | r | r | - | - | - | - | - | r | r | r | - | r | rwcsx | rwcsx | r | - | - | - | r |
| Job Card | - | r | - | - | - | rwcsx | - | rwcsx | rw | - | rwcsx | - | - | rwcsx | rwcsx | r | - | - | - | - |
| BOM | - | r | - | r | - | rwcsx | - | - | - | r | rwcsx | r | rwc | rwcsx | rwcsx | - | - | - | - | r |
| Item | r | r | r | r | r | rwc | - | - | r | r | r | - | rwc | r | r | r | - | r | - | r |
| Supplier | r | - | r | r | - | - | r | - | - | r | r | rw | r | r | - | r | - | - | r | r |
| Customer | r | - | r | - | rwc | - | r | - | - | r | r | - | - | r | - | - | - | rwc | - | r |
| Bank Account | rwc | - | - | - | - | - | rwc | - | - | - | - | - | - | - | - | - | - | - | r | - |
| Quality Inspection | - | r | r | - | - | - | - | - | - | - | - | - | - | - | - | rwcs | rwcsx | - | - | - |
| Quality Hold | r | r | r | r | r | r | r | rwcx | r | r | r | r | r | r | rwc | rwc | rwcx | r | r | r |
| Engineering Change Order | r | r | r | r | r | rwcsx | r | rwcsx | r | r | r | r | rwcs | r | r | r | r | r | r | r |
| Approval Policy | r | r | r | r | r | r | rw | rwc | - | r | r | r | r | r | r | r | r | r | r | r |
| Approval Request | r | r | r | r | r | r | r | r | - | r | r | r | r | r | r | r | r | r | r | r |
| Andon Event | r | r | r | r | r | r | r | rwc | rc/w | r | rwc | r | r | r | rwc | rc/w | rw | r | r | r |
| Kanban Card | r | r | r | r | r | r | r | rwc | r | r | rwc | r | r | r | rwc | r | r | r | r | r |
| Vehicle Unit | r | r | r | r | r | r | r | rw | r | r | r | r | r | rw | r | r | r | r | r | r |
| Shipment Manifest | r | r | r | r | r | r | r | rwcsx | r | rwcsx | r | r | r | r | r | r | r | r | r | rwcs |
| Advance Shipping Notice | r | r | r | rwcsx | r | r | r | rwcsx | r | r | r | rwcsx | r | r | r | r | r | r | r | r |
| Landed Cost Voucher | rwcs | r | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |
| Period Close Checklist | - | r | r | - | - | - | rwcsx | rwcsx | - | - | r | r | - | - | - | - | - | r | - | - |
| Audit Event | - | r | - | - | - | - | - | r | - | - | - | - | - | - | - | - | - | - | - | - |
| Integration Partner | - | r | - | - | r | - | - | rwc | - | - | - | r | - | - | - | - | - | - | - | - |
| Putaway Rule | - | r | - | - | - | - | - | - | - | rwc | rwc | - | - | r | - | - | - | - | - | r |
| Shift Handover | r | r | r | r | r | r | r | rwcsx | r | r | r | r | r | r | rwcsx | r | r | r | r | r |

Approval Request is created and advanced only by the approval service; its read right
lets an approver open a request from the inbox. Audit Event is append-only for everyone.

## Restricted fields and exports (SEC-4)

Data minimization for exports and reports. Fields of permission level 1 reach only the roles
granted that level (`RESTRICTED_FIELDS` and `PERMLEVEL_1` in `automotive_core.setup.permissions`).
Frappe leaves them out of every read of a user without the level: the form and
`/api/resource`, lists and the report view, `frappe.client.get_list`/`get_value`, and the
exports (the report view export drops the columns, the Data Export tool leaves them empty).
The Data Export tool reads child tables without the field level of their table field, so a
DocType with a restricted table (Employee: education, work history) keeps its export right with
the readers of that level; `TestDataMinimization` checks it.

| Restricted fields | Level 1 granted to | Personas that read them | ... and change them |
|---|---|---|---|
| Bank Account: account number, IBAN, branch code | read: Treasury Approver, Group Controller; read and write: AP Clerk, Accounts Manager, System Manager | Treasury Approver, AP Clerk, Group Controller | AP Clerk, Group Controller |
| Employee: gender, date of birth, emergency contact, contact details other than the company email (mobile, personal and preferred email, addresses), passport, marital status, blood group, family background, health, education, work history, salary, bank details, terms of employment (confirmation, contract end, notice, retirement), resignation and exit | read and write: HR User, HR Manager | none | none |

ERPNext gives every user linked to an Employee record the "Employee" role, which reads all
employees: colleagues keep the directory (name, company, department, designation, plant,
reports to, user, company email) and nothing else. HR staff hold ERPNext's HR roles, which no
persona includes: not even an IT Operator reads an employee's personal details. ERPNext copies
the date of birth and gender to the User record, which only System Managers read.

**Exports.** An export needs the *export* right on its DocType: the report view and list
(Menu → Export), the Data Export tool, and the download of a query or script report (which
checks the right on the report's DocType). Each export is recorded in the **Access Log**
(user, DocType or report, file type, filters, time): by Frappe for the report view and Data
Export, by `automotive_core.governance.exports` for report downloads (Frappe does not log
those; the desk's download path runs it). Log Settings does not clear the Access Log; the
[access review](../runbooks/access-review.md#periodic-access-review) goes through it, and only
System Managers (IT Operators) read it. The
rights of each persona on the DocTypes above, ERPNext's own except where the automotive grants
add rights (they never add *export*):

| Persona | Exports |
|---|---|
| AP Clerk | Payment Entry, Item, Bank Account |
| Auditor | Item, Approval Policy, Approval Request, Period Close Checklist, Audit Event, Integration Partner |
| Automotive Executive | Item |
| Buyer | Item, Advance Shipping Notice |
| Dealer Liaison | Item |
| Engineering Manager | Pick List, Job Card, Item, Engineering Change Order |
| Group Controller | Payment Entry, Journal Entry, Item, Bank Account, Approval Policy, Period Close Checklist |
| IT Operator | Job Card, Item, Quality Hold, Engineering Change Order, Approval Policy, Approval Request, Andon Event, Kanban Card, Vehicle Unit, Shipment Manifest, Advance Shipping Notice, Period Close Checklist, Audit Event, Integration Partner, Shift Handover |
| Line Operator | Item |
| Logistics Coordinator | Delivery Note, Pick List, Item, Shipment Manifest, Advance Shipping Notice |
| Plant Director | Pick List, Job Card, Item, Andon Event, Kanban Card, Shift Handover |
| Procurement Manager | Material Request, Item, Advance Shipping Notice |
| Product Engineer | Item, Engineering Change Order |
| Production Planner | Pick List, Work Order, Job Card, Item, Vehicle Unit |
| Production Supervisor | Pick List, Work Order, Job Card, Item, Quality Hold, Andon Event, Kanban Card, Shift Handover |
| Quality Inspector | Item, Quality Hold, Andon Event |
| Quality Manager | Item, Quality Hold, Andon Event |
| Sales Manager | Sales Order, Item |
| Treasury Approver | Item |
| Warehouse Clerk | Pick List, Item, Shipment Manifest, Advance Shipping Notice |

Every desk user may export the Item list at its *select* level (code, name and the search
fields): ERPNext's own right. Portal users have no desk and export nothing. `test_permission_matrix`
checks this table against the live tables as well.

**Reports.** The reports, dashboards, pages, portals and APIs of the production apps select the
columns they show, and none uses a restricted field: `TestDataMinimization` fails on a new
use until it is reviewed and listed (today: the bank account change control and the inbound
bank statement match, which use the account number without showing it). Company, plant and
partner scope apply to reports and exports as to lists
(`test_an_export_holds_only_the_user_s_own_plant`).
