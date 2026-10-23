# Implementation Plan

Dependency-ordered milestones for Toyota ERP Clone. Waves follow §9 of the
requirements brief; a wave is "done" only when its exit gate has recorded
evidence (see `REQUIREMENTS_TRACEABILITY.md` and `ops/evidence/`).

Status legend: `[x]` done with evidence · `[~]` in progress / partial · `[ ]` not started.

## Wave 0 — foundation, version lock, local and CI environment

Exit gate: a fresh developer can boot ERPNext plus the custom apps reproducibly.

- [x] Upstream lock (`infra/versions.env`): Frappe v16.53.0, ERPNext v16.52.1,
      frappe_docker v4.1.1, image digests; commits verified after every clone.
- [x] Host setup script for Docker in a resource-capped slice (`scripts/host/install-docker-ubuntu.sh`).
- [x] Development stack (`infra/dev/compose.yaml`, `scripts/dev/up.sh`, idempotent
      `scripts/dev/bootstrap-bench.sh`): MariaDB 11.8, Redis cache + durable queue, bench.
- [~] App skeletons: `automotive_core`, `automotive_manufacturing`,
      `automotive_supply_chain`, `automotive_portals`, `automotive_seed`.
- [~] Schema-as-code generator (`scripts/dev/schema/generate.py`) with `--check` for CI.
- [ ] CI workflow (lint, unit, integration, build, smoke) runnable locally via `scripts/ci/`.

## Wave 1 — companies, master data, accounting, access control, seed pipeline

Exit gate: core records, fiscal configuration and permission matrices verified.

- [ ] Tenant bootstrap from `config/tenant/hinode.json` (3 companies, CoA, fiscal years,
      plants, ≥15 warehouses, departments, cost centers, branches, holiday lists, shifts).
- [ ] Plant / Production Line / Shift Pattern / Plant Assignment DocTypes; plant custom fields
      on transactional DocTypes; User Permission based company/plant/warehouse scoping.
- [ ] Persona role templates (16 internal + supplier/dealer), Role Profiles, Custom DocPerms,
      privileged-role 2FA, SoD rules.
- [ ] Approval engine (policy → request → immutable decisions, maker/checker, delegation with
      effective dates, document-hash binding) applied to governed DocTypes.
- [ ] Hash-chained Audit Event log; correction-reason enforcement on cancel/amend.
- [ ] Duplicate prevention (supplier/customer tax id + fuzzy name), master data change approval.
- [ ] Seed pipeline skeleton: profiles, deterministic RNG, resumable stages, invariant checks.

## Wave 2 — engineering BOMs, demand/MRP, work orders, manufacturing operations

Exit gate: production and material ledger transactions reconcile.

- [ ] Vehicle models, variant attributes, compatibility rules, Vehicle Configuration.
- [ ] BOM revision fields (revision, effectivity, supersession), scrap factors, approved substitutes.
- [ ] ECR / ECO with impact assessment, maker/checker approval, BOM revision creation,
      effectivity-driven BOM selection for new Work Orders, pinned history, guarded BOM Update Tool.
- [ ] Planning Parameter Set (versioned) + Planning Run (inputs snapshot, deterministic netting,
      shortage/exception report, capacity check) on top of Sales Forecast → MPS → Production Plan.
- [ ] Daily Plant Schedule + Line Sequence (mixed-model leveling, takt, shift calendars,
      capacity conflicts, rescheduling audit).
- [ ] MES API (idempotent operation reports), Vehicle Unit with separate state machines,
      genealogy capture from Serial and Batch Bundles, WIP / variance / OEE reports.

## Wave 3 — procurement, Kanban, warehouse/logistics, supplier portal

Exit gate: pull replenishment and purchase-to-receipt work end to end.

- [ ] Supplier Onboarding, Approved Supplier List, Sourcing Decision, purchase approval matrix.
- [ ] Supplier Delivery Schedule (versioned), PO acknowledgement / revised promise, ASN,
      receipt from ASN, Receipt Discrepancy, returns.
- [ ] Kanban Loop / Card / Signal, consumption-triggered and scan-triggered signals, JIT windows,
      stockout escalation.
- [ ] Three-way match with tolerances + Invoice Match Exception workflow.
- [ ] Cycle counts, inventory position views, consignment withdrawal, transport booking,
      shipment manifest with proof of dispatch/delivery, transport exceptions.
- [ ] Supplier portal (RFQs, quotations, orders, acknowledgements, ASNs, notices, remittances).

## Wave 4 — quality/Andon, genealogy, dealer orders and distribution

Exit gate: plant-stop/release and vehicle order-to-delivery flows work.

- [ ] Andon Event lifecycle, Quality Hold (jidoka) enforcement in Stock Entry, Job Card,
      Work Order, Pick List, Delivery Note, Sales Invoice, allocation.
- [ ] Nonconformance (extended ERPNext Non Conformance), Quality Disposition (stock effects),
      CAPA via Quality Action, closure approval, immutable inspection history.
- [ ] Recall impact query (lot → vehicles), genealogy tree, where-used.
- [ ] Dealer Registration, Dealer Order (portal) → Sales Order, promise dates, allocation rules,
      Vehicle Allocation with serial Stock Reservation Entries, change/cancel requests,
      delivery acceptance, credit notes, warranty metadata, discount approval.
- [ ] Dealer portal (orders, configuration, inventory, invoices, shipments, support).

## Wave 5 — finance, intercompany, integrations, dashboards, localization

Exit gate: the ten cross-domain journeys pass.

- [ ] Payment dual approval, restricted supplier bank details, period-close checklist,
      intercompany reconciliation, consolidation views, PPV / production variance reports.
- [ ] Intercompany flows (Kyushu → Aichi powertrains; Aichi → North America vehicles in USD)
      with transfer price agreements and goods in transit.
- [ ] Integration hub: inbound inbox (idempotent), transactional outbox, signed webhooks with
      retry/backoff/replay protection, local EDI-style adapters, OpenAPI contracts.
- [ ] KPI catalog (formula, sources, authorization filter, refresh, reconciliation) + dashboards,
      persona workspaces, Japanese localization.
- [ ] Ten journeys J-01..J-10 as automated integration tests and browser e2e suites.

## Wave 6 — scale, load, hardening, CI/release automation, recovery

Exit gate: all acceptance gates and operational runbooks verified.

- [ ] `standard` dataset load with invariant validation; `small` in CI; `large` documented.
- [ ] Immutable image build, production compose (TLS proxy, private networks, limits, health),
      backups (encrypted, off-host, verified restore), monitoring and alerting.
- [ ] Load tests (50 users, 150 peak) with recorded results; slow-query/index review.
- [ ] Install from a clean host, reboot persistence, upgrade/rollback and restore drills.
- [ ] Security: static analysis, dependency audit, SBOM, configuration review, findings log.
- [ ] Final traceability matrix.

## Working notes

- Host constraint (development machine only): the ERP stack shares a box with a production
  LLM service; everything runs in `erp.slice` (8 GB high / 10 GB max). Performance numbers from
  this host are labelled as such and are not the reference-hardware results.
