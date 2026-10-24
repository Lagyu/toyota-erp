# Implementation Plan

Dependency-ordered milestones for the Hinode Automotive Group ERP ("Toyota ERP Clone").
Waves follow §9 of the requirements brief; a wave is done only when its exit gate has
recorded evidence (see [REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md)).

Status: `[x]` done with evidence · `[~]` in progress / partial · `[ ]` not started.
Last updated 2026-10-24.

## Wave 0 — foundation, version lock, local and CI environment

Exit gate: a fresh developer can boot ERPNext plus the custom apps reproducibly. **Met.**

- [x] Upstream lock (`infra/versions.env`): Frappe v16.53.0, ERPNext v16.52.1,
      frappe_docker v4.1.1, image digests; commits verified after every clone.
- [x] Host setup script for Docker in a resource-capped slice (`scripts/host/install-docker-ubuntu.sh`).
- [x] Development stack (`infra/dev/compose.yaml`, `scripts/dev/up.sh`, idempotent
      `scripts/dev/bootstrap-bench.sh`): MariaDB 11.8, Redis cache + durable queue, bench, worker.
- [x] Apps `automotive_core`, `automotive_manufacturing`, `automotive_supply_chain`,
      `automotive_portals`, `automotive_seed`.
- [x] Schema-as-code generator (`scripts/dev/schema/generate.py`) with `--check` in CI.
- [x] CI workflow (lint, unit, integration, audit, image, SBOM, smoke, e2e) runnable locally via `scripts/ci/`.

## Wave 1 — companies, master data, accounting, access control, seed pipeline

Exit gate: core records, fiscal configuration and permission matrices verified. **Met.**

- [x] Tenant configuration as code (`apps/automotive_core/automotive_core/setup/tenants/hinode.yaml`):
      3 companies, chart of accounts, fiscal years, plants, ≥15 warehouses, departments, cost
      centers, branches, holiday lists, shifts, settings, approval policies, SoD rules.
- [x] Plant / Production Line / Shift Pattern / Plant Assignment; plant fields on transactional
      DocTypes; company/plant User Permission scoping (strict).
- [x] Persona role templates (16 internal + supplier/dealer), role profiles, permissions,
      privileged two-factor authentication, SoD rules and review report, user provisioning.
- [x] Approval engine (policy → request → immutable decisions, maker/checker, delegation,
      document-hash binding) on governed DocTypes; approval inbox.
- [x] Hash-chained Audit Event log; cancellation with reason.
- [x] Duplicate prevention for suppliers/customers; verified supplier bank accounts.
- [x] Seed pipeline: profiles, deterministic RNG, resumable stages, invariant checks.

## Wave 2 — engineering BOMs, demand/MRP, work orders, manufacturing operations

Exit gate: production and material ledger transactions reconcile. **Met** (small dataset validation).

- [x] Vehicle models, option rules, Vehicle Configuration, variant resolution.
- [x] BOM revisions (revision, effectivity, supersession), guarded BOM Update Tool.
- [x] ECR (impact assessment of orders, stock, work in progress, open requests and planning
      proposals) → ECO (approval, new revisions, parent ripple, effectivity-driven BOM selection,
      pinned history; operation changes as routing versions; approved alternatives, enforced at
      consumption).
- [x] Planning Parameter Set (versioned, approved) + Planning Run (stored snapshot, deterministic
      engine with BOM yield and the native MPS as forecast, shortages and capacity exceptions,
      replay, release through Production Plan); weekly production plan report (the weekly MPS).
- [x] Plant Schedule (mixed-model leveling, takt, audited resequencing).
- [x] MES API (idempotent unit/batch/operation reports), Vehicle Unit states, genealogy from
      serial/batch bundles (incl. auto-numbered outputs), WIP / OEE / variance reports.

## Wave 3 — procurement, Kanban, warehouse/logistics, supplier portal

Exit gate: pull replenishment and purchase-to-receipt work end to end. **Met** (J-02, J-06).

- [x] Supplier Onboarding, Approved Supplier List, Sourcing Decision, purchase approval matrices.
- [x] Supplier Delivery Schedule (versioned), acknowledgements / revised promises, ASN,
      receipt from ASN, Receipt Discrepancy, returns.
- [x] Kanban Loop / Card / Signal, consumption and scan signals, call-off release, escalation.
- [x] Three-way match with tolerances + Invoice Match Exception workflow.
- [x] Cycle counts, inventory position, consignment, transport booking, shipment manifest
      with proof of dispatch/delivery, transport exceptions.
- [x] Supplier portal (RFQs, quotations, orders, acknowledgements, schedules, ASNs, receipts,
      invoices, notices).

## Wave 4 — quality/Andon, genealogy, dealer orders and distribution

Exit gate: plant-stop/release and vehicle order-to-delivery flows work. **Met** (J-01, J-05, J-08, J-09).

- [x] Andon lifecycle; Quality Hold enforcement in stock, production and shipping documents.
- [x] Nonconformance with CAPA and closure rules, Quality Disposition (stock effects), inspection
      history, re-inspection after rework.
- [x] Recall analysis (lot → vehicles), genealogy tree, where-used.
- [x] Dealer Registration, Dealer Order → Sales Order, promise dates, allocation rules (build to
      order first), Vehicle Allocation with serial reservations, change/cancel requests, delivery
      acceptance, warranty start, discount approval.
- [x] Dealer portal (configure and order, orders, vehicles, deliveries, invoices, support).

## Wave 5 — finance, intercompany, integrations, dashboards, localization

Exit gate: the ten cross-domain journeys pass. **Met for the journeys** (browser suite
green on the small dataset, local reference stack); a green run on hosted CI is pending.

- [x] Payment dual approval, restricted bank details, period-close checklist, intercompany
      reconciliation, consolidation view (D-021), PPV / production variance reports.
- [x] Intercompany flows (Kyushu → Aichi powertrains; Aichi → North America vehicles) with
      transfer price agreements; goods in transit; landed costs on imported vehicles.
- [x] Taxes by party tax category (Tax Rules, zero-rated export, resale exemption, import).
- [x] Bank statements from a bank partner reconciled with payments and journals.
- [x] Integration hub: inbox, outbox, signed webhooks, retries, replay protection, EDI and
      file-drop adapters, desk replay/retry; carrier and supplier-invoice messages; versioned
      OpenAPI contract (D-034) with contract and authorization tests against the running API,
      and concurrency tests (one Idempotency-Key, simultaneous redeliveries, simultaneous
      submits); per-user request limits on the portals and the shop floor.
- [x] KPI catalogue and dashboards, persona workspaces; notifications with templates in the
      recipient's language, preferences and auditable dispatch; approval escalation.
- [x] Japanese and English: complete catalogues for the custom apps and the upstream strings
      of the desk and the standard documents; a check fails CI on any untranslated string.
- [x] Print formats for manifests, ASNs, kanban cards and vehicle certificates.
- [x] Ten journeys J-01..J-10 as integration tests and browser suites (`e2e/tests/`).

## Wave 6 — scale, load, hardening, CI/release automation, recovery

Exit gate: all acceptance gates and operational runbooks verified. **Open.**

- [~] Datasets: `small` generated and validated in CI, and `closing` (the small volumes across
      the fiscal year end) validated; `standard` generated on hosted runners in chained,
      resumable segments (`dataset.yml`, D-027, up to 18): the run on the current generator in
      progress (the first ones stopped at the first month-end and the year end); `large` defined.
- [x] Immutable image (runtime without the asset toolchain or package installers),
      production compose (TLS proxy, private networks, limits, health), encrypted off-host
      backups with verified restore drills, monitoring and alerting.
- [x] Latency alerting: request-duration histogram and the `ErpReadLatencyHigh` / `ErpWriteLatencyHigh` alerts.
- [~] Load tests: scenarios, runner and workflow ready (`perf/`, `perf.yml`); results await the
      standard dataset; slow-query/index review after them.
- [x] Production rehearsal on a fresh runner (`rehearsal.yml`): install by the runbook with
      TLS, monitoring, ClamAV and S3 backups, upgrade from a previous release, HTTPS checks,
      upload scanning, structured logs, simulated reboot, backup and point-in-time restore drill,
      rollback to the previous release: all 18 steps pass (the reboot is a Docker daemon restart).
- [~] Security: lint, dependency audit and image scan with reviewed allowlists, SBOM and licence
      inventory, findings log, configuration review, permission matrix checked by tests, scoped
      actions (C-10) kept by a lint check; owner decisions on the accepted findings open (C-03,
      V-02, V-03c/d/g, V-05); C-02 fixed, C-08 mitigated; the account expiry (C-11) and the
      scan gate of private file downloads (C-12) fixed after the hosted runs found them.
- [x] Documentation: README, ARCHITECTURE, DECISIONS, CHANGELOG, operations runbooks,
      performance procedure; the traceability matrix with the hosted runs' results
      (REQUIREMENTS_TRACEABILITY.md; the standard dataset and load test results follow).

## Working notes

- Containers and browsers run only on GitHub-hosted runners (D-026): the development machine's
  memory belongs to other work. Lint and translation tooling run locally.
- Performance results come from a hosted runner (4 vCPU, 16 GB, stack and load generator on
  one machine), not the brief's reference hardware; they are labelled as such.
