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
bank details) sit at permission level 1.

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
