# Architecture

How the Hinode Automotive Group ERP is put together: the platform it stands on, how the
custom applications divide the domain, how business data flows between them, and the
cross-cutting mechanisms (governance, security, integration, operations) every domain
relies on. Design choices are argued in [DECISIONS.md](DECISIONS.md); requirement
coverage is in [REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md).

## 1. Platform

| Layer | Choice (pinned in [`infra/versions.env`](infra/versions.env)) |
|---|---|
| Application framework | Frappe Framework v16.53.0 (commit verified after every clone) |
| ERP base | ERPNext v16.52.1 |
| Runtime | Python 3.14, Node 24 (asset build only) |
| Database | MariaDB 11.8 |
| Cache and queues | Redis 8.6 (separate cache and durable queue instances) |
| Container distribution | official `frappe_docker` v4.1.1 production compose + Hinode overrides, immutable application image |
| Edge | Traefik v3.7 (TLS, security headers), frappe_docker nginx frontend |
| Operations jobs | `cron` service on the application image (backups, restore drills; `ops/backup/cron.py`) |
| Optional services | ClamAV 1.5 (upload scanning), Prometheus / Alertmanager / Grafana with MySQL, Redis and node exporters |

ERPNext is not forked. Everything custom lives in versioned Frappe apps and reaches
ERPNext only through supported mechanisms: DocTypes, custom fields, `doc_events` hooks,
whitelisted methods, patches, fixtures and permission records.

## 2. Applications and boundaries

```text
            automotive_portals            supplier and dealer portals (www pages + JSON API)
                    │
          automotive_supply_chain         suppliers, procurement controls, logistics,
                    │                     dealers and allocation, intercompany
         automotive_manufacturing         engineering, planning/MRP, MES, shop floor,
                    │                     quality, traceability
             automotive_core              governance, audit, access, integration hub,
                    │                     finance controls, KPIs, operations, tenant setup
         ERPNext v16 / Frappe v16

   automotive_seed (reference images only) — synthetic data generator using all of the above
```

Dependencies point downwards only: a lower app never imports from a higher one. Where
a lower layer needs behaviour from a higher one it publishes a hook that the higher app
fills — `automotive_inbound_handlers` (message type → handler), `automotive_period_close_checks`,
`automotive_kpi_definitions`, `automotive_metrics`, `period_closing_doctypes`.

| App | Owns | Key DocTypes |
|---|---|---|
| `automotive_core` | approvals and delegation, segregation of duties, hash-chained audit trail, cancellation with reason, user provisioning, plants/lines/shifts, partner user access, integration hub, period close, KPI catalogue, file policy, health/metrics, tenant configuration | Approval Policy/Request/Decision/Delegation, Audit Event, Segregation of Duties Rule, User Provisioning Request, Plant, Production Line, Shift Pattern, Plant Assignment, Partner User Access, Integration Partner/Inbound Message/Outbound Message, Idempotency Record, Period Close Checklist, KPI Definition/Snapshot, Attachment Policy Rule, Automotive Settings |
| `automotive_manufacturing` | vehicle models and configuration, BOM revisions and engineering change, planning runs and line sequencing, MES reporting, Kanban, andon, quality holds/dispositions, genealogy and recall | Vehicle Model, Vehicle Option Rule, Vehicle Configuration, Engineering Change Request/Order, Planning Parameter Set, Planning Run, Plant Schedule, Vehicle Unit, Kanban Loop/Card/Signal, Andon Event, Shift Handover, Quality Hold, Quality Disposition, Genealogy Link, Recall Campaign |
| `automotive_supply_chain` | supplier onboarding and sourcing, delivery schedules and call-offs, acknowledgements and ASNs, three-way match, receiving discrepancies, cycle counts, consignment, transport and manifests, dealer registration/orders/changes, allocation, delivery acceptance, transfer pricing | Supplier Onboarding, Approved Supplier List, Sourcing Decision, Supplier Delivery Schedule, Supplier Order Acknowledgement, Advance Shipping Notice, Invoice Match Exception, Receipt Discrepancy, Cycle Count, Consignment Agreement/Movement, Transport Booking, Shipment Manifest, Dealer Registration, Dealer Order, Dealer Order Change, Vehicle Allocation, Delivery Acceptance, Transfer Price Agreement |
| `automotive_portals` | the two partner portals: pages, scripts, styles and their JSON APIs | — (uses the documents above) |
| `automotive_seed` | profiles, simulation, validation, end-to-end fixtures | Seed Run, Seed Run Stage |

### Source of truth

Standard ERPNext documents remain the record for everything ERPNext already models:
Item, BOM, Work Order, Job Card, Stock Entry, Material Request, Purchase Order, Purchase
Receipt, Purchase Invoice, Sales Order, Delivery Note, Sales Invoice, Payment Entry,
Stock Reservation Entry, Serial No, Batch, GL Entry, Stock Ledger Entry. Custom
DocTypes model what ERPNext lacks (a Kanban card, an andon stop, a quality hold, a VIN's
life) and link to the standard documents; they never hold a second copy of quantities,
values or balances. Ledgers are only ever written by ERPNext's own posting logic.

### Schema as code

DocType definitions are Python specifications in `apps/*/schema/*.py` (a small DSL in
[`scripts/dev/schema/`](scripts/dev/schema/)); `generate.py` writes the DocType, workspace,
sidebar and number-card JSON, and `generate.py --check` fails CI when the JSON drifts.
Data changes that accompany a release are Frappe patches (`apps/*/*/patches.txt`).

## 3. Business flows

The ten mandatory journeys, as they move through documents. Every arrow is an ordinary
document created by a person with the right role (or by a recorded system step on their
behalf), and every journey runs in the browser suite ([`e2e/tests/`](e2e/tests/)).

**J-01 dealer order to cash.** Dealer configures in the portal → Vehicle Configuration →
Dealer Order → (liaison accepts) Sales Order at the dealer price → planner's Work Order
for that order line reserves a VIN (Vehicle Unit) → MES `report_unit_built` posts the
Manufacture Stock Entry (genealogy recorded) → final Quality Inspection releases the
unit → allocation reserves that VIN for its order (Vehicle Allocation + serial Stock
Reservation Entry) → Delivery Note → Transport Booking + Shipment Manifest → proof of
delivery → dealer's Delivery Acceptance (warranty starts) → Sales Invoice → Payment Entry.

**J-02 pull replenishment.** Consumption at line side (or a card scan) empties a Kanban
Card → Kanban Signal → Material Request → call-off Purchase Order against the blanket
order (no new approval) → supplier acknowledges and announces an ASN (portal or EDI) →
Purchase Receipt from the ASN into line side → signal received, card full.

**J-03 planning.** Planning Parameter Set (approved, versioned) + demand → Planning Run
(snapshot stored, deterministic engine, shortages and capacity conflicts as exceptions,
replay verifies identical outputs) → planner approves proposals → release creates a
Production Plan, planned Work Orders for the plant's lines and submitted Material Requests.

**J-04 engineering change.** Engineering Change Request (impact assessment: BOMs, open
orders, stock) → Engineering Change Order → approval (engineering, then quality) →
submit creates new BOM revisions with an effective date (and rippling parent revisions);
Work Orders pick the revision effective on their start date, built orders stay pinned.

**J-05 defective batch.** Rejected inspection → Quality Hold (one per target) +
Nonconformance → every stock movement of the batch except into quarantine is refused →
andon stop (workstation hold, joins the open case) → containment → Quality Disposition
(approved by a second quality manager) executes a scrap, transfer or purchase return →
root cause and CAPA close the case → hold release → restart authorised by quality.

**J-06 procure to pay.** Purchase Invoice from the receipt → three-way match (quantity
billed vs accepted, price vs order within tolerances) → Invoice Match Exceptions block
submission until accepted, rejected or corrected → Payment Entry to a verified supplier
bank account → dual approval by two different treasury approvers → payment.

**J-07 intercompany.** Aichi Purchase Order at the transfer price (agreement checked) →
Kyushu Sales Order mapped from it (hand-off) → Kyushu Delivery Note → Aichi Purchase
Receipt mapped from the delivery (hand-off; the receiver never reads the other
company's documents) → Kyushu Sales Invoice → mirrored Aichi Purchase Invoice →
reconciliation of stock, receivable and payable.

**J-08 traceability.** Genealogy Links (component serial/batch → produced serial/batch,
written when a Manufacture entry posts) give the upstream tree of a VIN down to supplier
lots inside sub-assemblies; the reverse walk lists every unit built from a lot; a Recall
Campaign stores the result with query and result fingerprints so the analysis is
reproducible.

**J-09 change after allocation.** Dealer's change or cancel request → validated against
the order state → applied as a recorded system step on the dealer's behalf: Sales Order
amended or cancelled, reservation and allocation released, vehicle back in stock;
inventory value unchanged.

**J-10 integration retry.** Signed inbound message → stored once (partner + message id)
→ processed by the handler as the partner's service user → transient failure: Failed
with backoff, retried by the scheduler or replayed by IT; duplicates return the stored
result; handlers also deduplicate on the partner's business reference → reconciliation
checks every processed message's document.

## 4. Cross-cutting mechanisms

### Governance

- **Approvals** ([`governance/approvals.py`](apps/automotive_core/automotive_core/governance/approvals.py)).
  An Approval Policy lists ordered steps by role for documents of a DocType (by company,
  amount range, category). A request binds to a SHA-256 of the protected fields; each
  Approval Decision is immutable and checked for authority (role or active delegation),
  maker/checker and distinct approvers. A wildcard `before_submit` hook refuses to submit
  a governed document without an approved request whose hash still matches.
- **Delegation** (Approval Delegation) with effective and expiry dates.
- **Segregation of duties**: SoD rules list incompatible roles; role assignment and user
  provisioning check them, and the SoD review report lists conflicts.
- **Audit trail** ([`governance/audit.py`](apps/automotive_core/automotive_core/governance/audit.py)).
  Audit Events are written in the business transaction, then sealed by a single sealer
  into a SHA-256 hash chain; database triggers protect sealed rows and `verify_chain`
  reports the first break. Approvals, cancellations, holds, andon transitions, allocations,
  integration failures and system steps all record events.
- **Cancellation with reason**: governed documents are cancelled through a dialog that
  requires a reason, which is recorded on the audit trail.
- **Recorded system steps** ([`governance/elevation.py`](apps/automotive_core/automotive_core/governance/elevation.py)).
  When a validated request by one person must change documents that person may not edit
  (a dealer's cancellation rewrites a Sales Order; an approved disposition creates a
  purchase return), the follow-up runs with system rights inside `on_behalf_of`, which
  records the system actor and the requester; the resulting documents are owned by the
  requester.

### Access control

- **Personas**: 16 internal roles and the supplier and dealer portal roles, combined into
  role profiles ([`setup/roles.py`](apps/automotive_core/automotive_core/setup/roles.py),
  [`setup/permissions.py`](apps/automotive_core/automotive_core/setup/permissions.py) and
  the DocType specs). Privileged roles require two-factor authentication.
- **Row scope**: User Permissions on Company and Plant (strict user permissions on), applied
  to standard and custom documents; reports and KPI computations apply the same scope
  ([`analytics/scope.py`](apps/automotive_core/automotive_core/analytics/scope.py)).
- **Partners**: portal users hold only the custom portal role. Their party (supplier or
  dealer) is resolved on the server from Partner User Access — never taken from the
  request — and every portal read and write is filtered by it. ERPNext's own Supplier and
  Customer roles are stripped from portal users.
- **Default deny**: new DocTypes and whitelisted methods start without permissions; each
  method checks its roles (`frappe.only_for`) and document permissions explicitly.

### Idempotency and exactly-once effects

State-changing portal and MES methods require an `Idempotency-Key`. The first call
inserts an Idempotency Record (unique scope = endpoint + user + key) in the same
transaction as its business effect and stores the response; a retry returns the stored
response, a concurrent duplicate waits on the unique index, and reusing a key for a
different request is refused (422). Inbound messages are deduplicated by partner and
message id, and handlers deduplicate on the partner's business reference as well.

### Integration hub

- **Inbound**: `POST /api/method/automotive_core.integration_hub.inbound.receive` with
  `X-Hinode-Partner` and `X-Hinode-Signature: t=…,kid=…,n=…,v1=HMAC-SHA256(secret, t.n.body)`;
  timestamp tolerance, durable nonce store, per-partner rate limit; JSON or Hinode EDI.
- **Inbox**: Integration Inbound Message (Received → Processed / Failed / Dead Letter /
  Rejected); handlers run as the partner's service user, inside a savepoint, so a failure
  leaves no partial effect; backoff doubles per attempt; IT operators replay from the desk.
- **Outbox**: Integration Outbound Message written in the business transaction, delivered
  by a scheduler with retries, backoff and dead-lettering; HTTPS webhooks only to
  allow-listed hosts and signed the same way; delivery attempts are recorded.
- **Adapters**: HTTPS webhook and local file drop. Partners without live credentials use
  the file-drop adapter, which processes messages end to end; nothing is presented as a
  live connection that is not one.
- **Correlation IDs** ([`observability/context.py`](apps/automotive_core/automotive_core/observability/context.py)):
  every request gets an id (or keeps a safe incoming `X-Correlation-ID`), jobs inherit it,
  messages and audit events store it, and logs are structured JSON.

### Files

Uploads are checked against attachment policy rules (allowed types and sizes per
DocType, private by default), scanned by ClamAV when the scanner service is deployed,
and attachments are readable only by users who can read the parent document.

### Management information

KPIs are defined in a catalogue (KPI Definition) contributed by each app: formula,
source DocTypes, authorization filter, refresh policy and a reconciliation check that
recomputes the value from its source. Workspaces show KPI number cards only to users who
can read their source; management reports apply the viewer's company and plant scope.

## 5. Synthetic data generator

`automotive_seed` builds a reference dataset by simulating the group day by day:
planning, receiving, production, quality, call-offs, dealer orders, deliveries,
invoicing, payments and month-end — each step posted through the normal documents and
APIs by the dataset person who owns it. Time is simulated (frozen per step, strictly
increasing within a day so stock postings keep their order); each day is one database
transaction and the run resumes from the last completed day. Background jobs run inline
and pending valuation reposts are processed daily. At the end, invariant checks verify
trial balances, stock ledger against GL, bins against the ledger, receivables and
payables against the payment ledger, intercompany balance, traceability, the audit chain,
exactly-once integration and the profile's volume targets.

The browser suite uses the same machinery: `automotive_seed.e2e` gives each persona a
one-run password and arranges journey preconditions (material at the line, a partner's
signing secret) by running the generator's own flows at the current time.

## 6. Deployment

| Environment | How it runs |
|---|---|
| Development | [`infra/dev/compose.yaml`](infra/dev/compose.yaml): MariaDB, Redis (cache + queue), a bench container with the repository mounted, a worker; sites `test.localhost` (tests) and reference sites for datasets |
| CI | GitHub Actions running [`scripts/ci/`](scripts/ci/): lint, unit, integration on the development stack, image builds, SBOM and scan, production smoke test, end-to-end suite on a reference stack |
| Staging / reference | production stack from the reference image (adds the generator), `ENVIRONMENT=staging` or `reference` |
| Production | production image (no generator), [`config/env/production.env.example`](config/env/production.env.example) |

The production stack is the official frappe_docker compose (backend, frontend,
websocket, short and long workers, scheduler, MariaDB, Redis cache and queue) assembled
by [`scripts/deploy/compose.sh`](scripts/deploy/compose.sh) with
[`infra/deploy/compose.hinode.yaml`](infra/deploy/compose.hinode.yaml): Traefik in front
(`TLS_MODE=letsencrypt` or `files`, with HSTS and security headers; `none` publishes the
frontend directly for local test stacks only), private networks, restart
policies, resource limits, health checks, durable volumes, a socket-free cron service for backups and
restore drills; optional ClamAV and monitoring overlays. The image is built once from the
pins (dependency constraints frozen in [`infra/image/constraints.txt`](infra/image/constraints.txt))
and promoted unchanged.

Health: `automotive_core.ops.health.live` (process up), `ready` (database, cache, queue,
scheduler) and `metrics` (Prometheus text, token header). Alerts cover error rates, failed
and stuck jobs, queue backlog, disk pressure, backup age and latency
([`infra/deploy/monitoring/alerts.yml`](infra/deploy/monitoring/alerts.yml)).

Backups: `bench backup` with files, encrypted into an off-host restic repository daily
with retention and partial read-back checks; a weekly restore drill restores into a
scratch site and verifies ledgers, the audit chain and record counts
([`ops/backup/`](ops/backup/)).

A single host is the delivered topology. High availability (replicated database,
several application hosts behind a load balancer, shared file storage) is a documented
option ([ops/runbooks/high-availability.md](ops/runbooks/high-availability.md)), not
packaged as settings and not rehearsed; the 99.9% availability objective is not claimed
for one host.

## 7. Known limitations

Tracked with their status in the "Open items" section of
[REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md).
