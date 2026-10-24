# Changelog

All notable changes. Versions follow [Semantic Versioning](https://semver.org/); the
application image is tagged `<version>-<git sha>`. Dates are ISO 8601.

## [Unreleased] — 0.1.0

First functional build of the Hinode Automotive Group ERP on ERPNext v16.52.1 /
Frappe v16.53.0. Not yet released: the acceptance gates still open are listed in
[REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md).

### Platform and delivery
- Pinned, commit-verified upstream set (Frappe, ERPNext, frappe_docker, base images by
  digest) in `infra/versions.env`.
- Development stack (MariaDB 11.8, Redis cache + durable queue, bench, worker) with an
  idempotent, resumable bootstrap.
- DocType schemas as code with generated JSON and a drift check.
- Immutable application image (production and reference flavours) with frozen Python
  constraints; production compose on frappe_docker v4.1.1 with Traefik (Let's Encrypt or
  file certificates), a socket-free cron service, restic backups and weekly restore drills, optional
  ClamAV and Prometheus/Alertmanager/Grafana overlays.
- Health (`live`, `ready`) and token-protected Prometheus metrics endpoints; alert rules
  for service health, queues, failed jobs, integration failures, audit sealing, error
  spikes, backup age and disk space.
- CI: lint (ruff, shellcheck, schema drift, YAML/JSON, identity boundary), unit and
  integration tests, dependency audit with a reviewed allowlist, image builds, SBOM and
  vulnerability scan, production-shaped smoke test with backup and restore drill, and the
  end-to-end suite on a reference stack with a cached synthetic dataset.

### Governance, access and integration (`automotive_core`)
- Approval engine (policies, hash-bound requests, immutable decisions, maker/checker,
  distinct approvers, delegation with expiry) enforced on submit; approval inbox.
- Hash-chained, sealed audit trail with database-trigger protection and verification;
  cancellation with a recorded reason; recorded system steps on a requester's behalf.
- Persona role templates and profiles, privileged two-factor authentication, company and
  plant row scope, partner access for supplier and dealer users, segregation-of-duties
  rules and review report, user provisioning requests with expiry.
- Integration hub: signed HTTPS inbound endpoint with replay protection and rate limits,
  idempotent inbox, durable outbox with retries/backoff/dead letters, HTTPS allowlist,
  Hinode EDI format, file-drop adapter, correlation IDs; replay and retry from the desk.
- Inbound partner secrets rotate with an overlap: the server generates the new secret and
  shows it once, the previous one is accepted until the overlap ends (at most 7 days), and
  each delivery still signed with it is logged.
- Approval escalation: a step pending past its escalation time reminds its approvers and
  alerts the next level (hourly). Attachments of DocTypes without a retention rule follow the
  default retention period.
- Item master duplicates: one active item per supplier part number and per name within an
  item group (a reviewed exception is recorded in the audit chain).
- Period close checklist (checks, waivers, approval, accounting period closing), group
  consolidation view, verified supplier bank accounts, KPI catalogue with reconciliation.
- Tax determination: tax categories (domestic, export zero-rated, resale exempt, import),
  zero-tax templates and Tax Rules per company, so documents created by code get their
  taxes; default categories for new dealers, suppliers and intercompany parties.
- Bank statements from a bank partner (signed `bank_statement` messages) become bank
  transactions that reconcile automatically with payment and journal entries.
- Request limits per signed-in user and minute for the partner portals (reads, changes and
  partner uploads) and the shop floor, set in Automotive Settings; HTTP 429 over the limit.
- The custom methods' OpenAPI contract is versioned (1.1.0): additions raise the minor
  version, breaking changes come as new paths with the old ones deprecated, and CI checks
  that the version follows each change of the contract (D-034).
- A licence inventory comes with each image's SBOM (`scripts/ci/licences.py`; committed copy
  `ops/security/licences.md`), and install-fresh-host.md lists in one table the external
  accounts and credentials a deployment needs and what is missing until each is configured.
- Upgrades put the site in maintenance mode before the pre-upgrade backup, so the backup is
  exactly the state being upgraded; a rollback restores it with `--no-replay` (the shipped
  binary logs after it hold the upgrade's own migration; upgrade.md said to replay them).
  The production rehearsal now rolls back to the previous release and checks the structured
  log under a request's correlation id; deliveries naming an unknown partner are logged.
- Reports are offered (workspaces, sidebars) only to the roles that may run them: a report
  whose roles go beyond those allowed to report on its DocType gets a Custom Role naming only
  those, re-applied on every migrate. The navigation check opens single DocTypes as forms and
  pages as the desk does, and covers all 20 internal role profiles.
- Actions that change a document past the DocType permissions (andons, hold release, MES
  operation completion, planning runs, plant schedules, dealer order acceptance, discrepancy
  and match-exception resolution, bank-account verification) act only on documents of the
  caller's own plant and company; building a plant schedule needs a planning role (finding
  C-10). A lint check keeps new actions to the rule, and API tests call every role-guarded and
  partner method as a signed-in user without the right.
- Notifications through one service: desk alerts and e-mails from editable templates in
  the recipient's language, per-user preferences, and a Notification Dispatch record of
  every delivery result.

### Manufacturing (`automotive_manufacturing`)
- Vehicle models, option rules and configurator; BOM revisions with effectivity; ECR with
  impact assessment and ECO with approval, new revisions and rippling parents.
- Versioned planning parameter sets; deterministic MRP with shortage and capacity
  exceptions, replay verification and release to production plans; leveled daily line
  sequence with audited resequencing.
- Vehicle Units with VINs reserved at release; MES reporting with idempotency keys;
  Kanban loops, cards and signals; andon with jidoka holds; shift handover; WIP, OEE,
  variance and genealogy reports.
- Quality holds enforced across stock, production and shipping documents; inspections,
  re-inspection after rework, nonconformance with CAPA closure rules, dispositions;
  multi-level genealogy and recall analysis.
- Inspection plans with measured parameters and acceptance ranges: ERPNext accepts or
  rejects from the readings.
- Routings with timed operations at the Kyushu stations; job cards with labour records
  (operation confirmations) gate posting; operating cost (labour and overhead per station
  hour) in the product valuation.

### Supply chain (`automotive_supply_chain`)
- Supplier onboarding, approved supplier list, sourcing decisions, purchase approval
  matrices; blanket orders and call-offs; versioned delivery schedules; order
  acknowledgements; ASNs; receipts from ASNs; receipt discrepancies; three-way match with
  an exception workflow.
- Cycle counts, consignment agreements and movements, transport bookings, shipment
  manifests with proof of dispatch and delivery, transport exceptions.
- Dealer registration and authorised models, dealer orders with configuration and promise
  dates, acceptance, build-to-order then rule-based allocation with serial reservations,
  change and cancellation requests, delivery acceptance and warranty start.
- Intercompany transfer price agreements, mapped seller orders and buyer receipts,
  intercompany reconciliation; Goods In Transit report.
- Landed costs: ocean freight and import duty ordered, invoiced and allocated to imported
  vehicles with Landed Cost Vouchers.
- Storage at the plants: put-away rules fill each forward store (the raw material store) up
  to a part's capacity and keep the rest in reserve storage; reorder rules on the forward
  store raise a daily transfer request from on-hand stock, which the warehouse clerk
  fulfils with a pick list from reserve storage, for what the forward store has room for.
- Print formats (A4, Japanese/English): shipment manifest with driver and receiver
  signature boxes, advance shipping notice, kanban card with a QR code, vehicle
  certificate. Every role that can read a document can print it.

### Portals (`automotive_portals`)
- Supplier portal: overview, RFQs and quotations, orders and acknowledgements, delivery
  schedules, ASNs with documents, receipts and discrepancies, invoices and remittances,
  notices.
- Dealer portal: overview, configure and order, orders and change requests, allocated
  vehicles, deliveries and acceptance, invoices, support requests.
- Accessible tabbed interface (keyboard navigation, live regions), deep links, tablet
  layouts, idempotent writes.
- Portal roles enforced: each partner user's role decides the portal functions, tabs and
  actions available to them.

### Localization
- Japanese and English user interfaces: complete Japanese catalogues for the custom apps,
  plus the upstream Frappe and ERPNext strings of the desk, the login pages and the
  standard documents the processes use (Frappe and ERPNext ship none); one shared
  glossary; lint checks completeness, placeholders and tags.

### Synthetic data (`automotive_seed`)
- Deterministic small, standard and large profiles generated by simulating the group day
  by day through the application, with invariant validation (ledgers, stock, payment
  ledger, intercompany, traceability, audit chain, exactly-once integration, volumes).
- End-to-end fixtures: one-run persona passwords and journey preconditions arranged
  through the generator's own flows.

### End-to-end acceptance
- Playwright suite: journeys J-01 … J-10, role surfaces of 13 personas, supplier and
  dealer portals and the shop-floor andon on desktop and tablet.

### Fixed during end-to-end testing
- Create ECO from a change request produced an invalid draft (no company, disposition or
  quantity); quantity changes that change nothing are refused.
- MRP runs with a capacity conflict failed to save (line reported as an item).
- Genealogy was not recorded for outputs numbered during posting (sub-assembly batches,
  engine and powertrain serials); backfill patch added.
- Delivery schedules could over-order a request holding several lines of one item and
  scheduled Kanban requests twice.
- Operators and inspectors could not fill the andon form (create without write).
- Purchase requests released through a production plan stayed drafts.
- Restore drill parsed escaped `bench execute` output; portal confirmations were cleared
  by the follow-up navigation; portal tabs ignored hash changes.
- Allocation could give a build-to-order vehicle to an older order of the same variant.
- ASNs were accepted against purchase orders on hold.
- Values that start with "-" (generated tokens, encryption keys) broke `bench set-config`;
  integration tests could start before the bench bootstrap finished.
- The North America export picked the same vehicles for two open orders; master data of
  the synthetic dataset was dated after the history that uses it.
- The synthetic opening stock exceeded the forward stores' put-away capacity (it now puts the
  excess in reserve storage), and the generator expedited parts that reserve storage held.
- Concurrent requests with one Idempotency-Key answered 417 instead of replaying the first
  answer; two processes could each create a site's encryption key, leaving values encrypted
  with the first undecryptable (sites now get their key at creation, or at the next upgrade);
  Prometheus could not scrape the application (Frappe refused the Authorization header); the
  inbound `dealer_order_change` handler failed on every message.
- The `cron` service had no restart policy (it did not come back after a reboot), and the
  Python that sets a stack's Administrator password ran from the bench directory, where
  Frappe's logger finds no log directory.

### Operations
- Point-in-time recovery: the binary logs and new uploads are shipped off the host every 5
  minutes; `restore.sh` and the weekly drill bring the nightly backup forward from them (to the
  newest shipped change or `--until`), and the drill records the recovery point it reached.
- Upgrades and restores wait until the site is ready before the smoke test: Frappe's workers
  keep the site config, including maintenance mode, for up to a minute.
- Dataset workflow: the standard profile generated on hosted runners in chained,
  resumable segments; the validated export is kept for the performance tests.
- Load tests (`perf/`, `scripts/perf/run.sh`, `perf.yml`): desk personas, partner portals
  and background MRP runs against the standard dataset, 50-user steady and 150-user peak
  phases, p95 checked against the targets (`ops/performance/README.md`).
- Permission matrix (`ops/security/permission-matrix.md`), checked against the live
  permission tables by the integration tests.

### Security
- Findings log with the image scan (SBOM, Trivy) and a configuration review
  (`ops/security/findings.md`); Content-Security-Policy header; the runtime image applies
  Debian security updates and carries no package installer.
- No container mounts the Docker socket: Traefik reads its routes from files and the backup
  and restore-drill schedules run in a socket-free `cron` service.
- Behind the TLS proxy the session cookie is marked Secure: gunicorn takes the forwarded
  scheme from the stack's own front end.
