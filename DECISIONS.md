# Decisions

Significant technical choices, why they were made, and what they cost. Numbers are
stable: code and documents refer to them (for example `D-021` in the group consolidation
report). New decisions are appended; a reversed decision is marked superseded, not
deleted.

---

## D-001 Build on ERPNext v16 / Frappe v16 at pinned, verified releases

**Context.** The brief requires ERPNext v16 on a verified, compatible release set, with
standard DocTypes as the source of truth and no fork.
**Decision.** Pin Frappe v16.53.0, ERPNext v16.52.1 and frappe_docker v4.1.1 by tag *and*
commit in `infra/versions.env`; every clone is verified against the commit
(`scripts/lib/verify-checkout.sh`); base images are pinned by digest. Custom behaviour
uses hooks, custom fields, patches and whitelisted methods only.
**Consequences.** Upgrades are explicit changes to one file, rehearsed with
`scripts/deploy/upgrade.sh`. Where ERPNext has a defect we work around it in our code and
note it (e.g. D-022) instead of patching ERPNext.

## D-002 Four domain apps plus a generator, dependencies pointing one way

**Decision.** `automotive_core` ← `automotive_manufacturing` ← `automotive_supply_chain`
← `automotive_portals`, plus `automotive_seed`. A lower app never imports a higher one;
when it needs their behaviour it defines a hook that higher apps fill
(`automotive_inbound_handlers`, `automotive_period_close_checks`,
`automotive_kpi_definitions`, `automotive_metrics`).
**Why.** The brief's provisional app split maps onto the domain dependencies (dealers need
vehicles, vehicles need governance); one-way imports keep each app testable on its own.
**Consequences.** Some flows cross apps through hooks rather than direct calls, which is
slightly more indirection.

## D-003 DocType schemas as code

**Decision.** DocTypes, workspaces, sidebars and number cards are specified in Python
(`apps/*/schema/*.py`, DSL in `scripts/dev/schema/`) and the JSON is generated;
`generate.py --check` fails CI on drift.
**Why.** Hand-edited DocType JSON drifts and is hard to review; specs are compact and
reviewable, and permissions sit next to the fields they protect.
**Consequences.** Never edit generated JSON; change the spec and regenerate.

## D-004 ERPNext documents are the record; custom DocTypes only for missing concepts

**Decision.** Items, BOMs, work orders, stock entries, purchase and sales documents,
payments, reservations, serials, batches and all ledgers stay standard. Custom DocTypes
(Kanban card, andon event, quality hold, Vehicle Unit, …) link to them and never hold a
second copy of quantities, values or balances.
**Consequences.** Every ledger is produced by ERPNext's posting code, so financial and
stock reconciliation holds by construction and is verified after data generation.

## D-005 Own approval engine instead of Frappe Workflow

**Decision.** Approval Policy → Approval Request → immutable Approval Decisions, enforced
by a wildcard `before_submit` hook. A request is bound to a SHA-256 of the fields the
policy protects; any later change invalidates it.
**Why.** The brief needs amount- and company-dependent steps, maker/checker, distinct
approvers, delegation with expiry and immutable accountability across many DocTypes;
Workflow's state machine per DocType does not express these and its states are editable.
**Consequences.** Governed documents show an Approval menu instead of workflow actions.

## D-006 Hash-chained audit log, sealed asynchronously

**Decision.** Audit Events are inserted in the business transaction without a hash; a
single sealer (MariaDB named lock) assigns chain positions and hashes later. Database
triggers block changes to sealed rows; `verify_chain` reports the first break.
**Why.** Hashing inside the transaction would serialise every writer on the chain head.
**Consequences.** Recent events are unsealed for a short time (alert `ErpAuditSealingBehind`).

## D-007 Row-level scope through User Permissions in strict mode

**Decision.** Company and plant scope are User Permissions (strict user permissions on);
reports and KPI queries apply the same scope through `analytics/scope.py`. Employees
created for users do not get ERPNext's automatic company permission (it would widen
cross-company roles).
**Consequences.** One mechanism protects desk lists, forms, links, reports and APIs.

## D-008 Portals as server-rendered Frappe pages with a small JSON API

**Decision.** `/supplier-portal` and `/dealer-portal` are Frappe www pages with plain
JavaScript modules and whitelisted API methods. The partner is resolved on the server
from Partner User Access; portal users get only the custom portal role (ERPNext's
Supplier and Customer roles are stripped on save).
**Why.** No extra frontend toolchain or service; the server decides what a partner sees.
**Consequences.** Portal screens are built by hand, accessible (tabs, keyboard, live
regions) and responsive, but not a full SPA.

## D-009 Idempotency records inside the business transaction

**Decision.** State-changing portal, MES and integration methods take an
`Idempotency-Key`; a record with a unique scope key is inserted in the same transaction
as the effect and stores the response.
**Consequences.** Retries are safe across crashes; a key reused for a different payload
is refused (422).

## D-010 Integration hub with signed HTTPS, inbox/outbox and a project EDI profile

**Decision.** HMAC-SHA256 signatures with timestamp and nonce; an inbox that stores each
message once and processes it as the partner's service user; an outbox written in the
business transaction with retries, backoff and dead-lettering; a segment-based "Hinode
EDI" profile (X12-inspired, not a standard implementation); a file-drop adapter for
partners without live credentials.
**Consequences.** No unconnected partner is presented as live. Mapping to a real EDI
standard is a partner-specific adapter for later.

## D-011 Deterministic MRP over stored snapshots

**Decision.** A planning run collects a snapshot of demand, supply, BOMs, lead times and
calendars, stores it, and runs a pure-Python engine (`engine/mrp.py`); outputs are
hashed and a replay from the stored snapshot must be identical. Release goes through
ERPNext's Production Plan.
**Consequences.** Planning is reproducible and testable without a database; capacity
overloads are reported as exceptions against the production line.

## D-012 Vehicle Unit as the VIN-level aggregate

**Decision.** VINs are reserved (Vehicle Unit, status Planned) when a vehicle work order
is submitted; the unit carries separate production, quality, inventory, shipment and
invoice states, kept in step by document hooks. The VIN is also the Serial No.
**Consequences.** One record answers "where is this vehicle and what state is it in".

## D-013 Genealogy captured when manufacture entries post

**Decision.** Genealogy Links (consumed serial/batch → produced serial/batch) are written
from the entry's Serial and Batch Bundles when it is submitted; outputs numbered during
posting are read back from the stored bundle. A backfill patch repairs entries posted
without links.
**Consequences.** Traceability is as deep as the tracked components (vehicle →
powertrain/sub-assembly → supplier lot).

## D-014 Quality holds enforced in document validation, one per target

**Decision.** An active Quality Hold blocks movements, production and shipping of what it
targets in the validation of Stock Entry, Job Card, Work Order, Purchase Receipt,
Delivery Note, Pick List and Sales Invoice; only segregation into quarantine/scrap and
disposition entries pass. A second finding on a held target confirms the existing hold.
**Consequences.** Jidoka is enforced by the server whichever screen or API is used.

## D-015 Allocation: build-to-order first, then rule-based FIFO

**Decision.** A released vehicle built for a sales order line serves that line; other
vehicles serve open lines by the company's allocation rule (order date, dealer tier or
requested date). Allocations hold the VIN with a serial-level Stock Reservation Entry.
**Consequences.** Build-to-order vehicles cannot be taken by older orders of the same
variant; releasing an allocation never changes inventory value.

## D-016 Recorded system steps instead of broad roles

**Decision.** When a validated request must change documents the requester may not edit
(dealer change → Sales Order; disposition → purchase return; intercompany hand-offs;
supplier quotations), the follow-up runs inside `governance.elevation.on_behalf_of`,
which records the system actor and the requester; the documents are owned by the
requester.
**Why.** Granting dealers or quality staff purchasing or sales rights would break least
privilege.

## D-017 Intercompany through transfer price agreements and mapped hand-offs

**Decision.** Group companies trade through ERPNext internal customers/suppliers;
Transfer Price Agreements fix prices; the seller's order and the buyer's receipt are
mapped from the counterpart document by `intercompany/handoff.py`, so neither side
retypes or reads the other company's documents.
**Consequences.** Both companies book matching events; a reconciliation compares them.

## D-018 Synthetic data by simulation through the application

**Decision.** The generator simulates the group day by day through the same documents and
APIs as users, each step posted by the dataset person who owns it, under a simulated
clock (frozen per step, strictly increasing) with one transaction per day, inline
background jobs and resumable state.
**Why.** The brief forbids inconsistent ledger inserts; simulation also exercises the code
the way production will.
**Consequences.** Generation is slow (minutes per simulated day at standard volume) but
the result passes the same invariants as live data.

## D-019 Production packaging on the official frappe_docker compose

**Decision.** Use frappe_docker's compose plus our overrides (Traefik with TLS modes,
scheduled jobs, restic backups, optional ClamAV and monitoring) and an immutable image
built from the pins with frozen Python constraints; images are promoted unchanged.
`pwd.yml` is never used.
**Consequences.** Upgrades follow upstream's container model; our overrides stay small.
**Amended.** The scheduled jobs first ran under Ofelia, which needed the Docker socket; the
socket-free `cron` service (`ops/backup/cron.py`) replaced it, and Traefik reads its routes
from files (findings C-02).

## D-020 KPI catalogue with reconciliation checks

**Decision.** Each KPI is a catalogue entry with formula, source DocTypes, authorization
filter, refresh policy and a reconciliation function that recomputes it from source.
Number cards are shown only to users who can read the source DocType.

## D-021 Group consolidation is a management view

**Context.** ERP-09 asks for group reporting across three companies in two currencies.
**Decision.** The Group Consolidation report translates each company's GL by root type
(balance sheet at the closing rate, profit and loss at the period's average rate),
shows the translation difference, and identifies intercompany balances and flows. It
does not post eliminations or maintain a consolidation ledger, and unrealised profit in
intercompany inventory is not eliminated.
**Why.** Statutory consolidation needs ownership structures, elimination journals and
audit sign-off that are out of scope; a GL-derived view gives management the group
picture without a second set of books.
**Consequences.** Figures are traceable to GL entries; statutory group accounts remain an
external process.

## D-022 Period close through a checklist and ERPNext Accounting Periods

**Decision.** A Period Close Checklist runs registered checks (core checks plus
app-contributed ones), allows waivers of waivable checks by a group controller with a
reason, requires approval, and on submit creates or re-enables an Accounting Period
closing every period-closing DocType; cancelling disables it.
**Note.** ERPNext's server-side bootstrap of closed document types mishandles its own
list, so the checklist fills `closed_documents` explicitly.

## D-023 Upstream-pinned dependency advisories are reviewed, not overridden

**Decision.** Advisories in libraries whose versions Frappe/ERPNext pin (Pillow, pypdf,
filelock, GitPython, pdfkit) are listed in `config/security/audit-allowlist.txt` with
reasons and review dates; `scripts/ci/audit.sh` fails on any advisory not listed.
**Why.** Overriding upstream pins risks breaking the framework; the exposure is reviewed
per advisory and removed when upstream updates.

## D-024 End-to-end tests run as dataset personas

**Decision.** The Playwright suite signs in as real users of the synthetic dataset
(passwords generated per run, never stored in the repository) and arranges journey
preconditions through the generator's own flows; in CI it runs against a reference stack
built from the image, with the generated dataset cached by generator version.
**Consequences.** Tests exercise real permissions and row scopes; CI regenerates the
dataset only when the generator changes.

## D-025 Licence: GPL-3.0-only, pending the owner's decision

**Decision.** The custom apps are GPL-3.0-only, compatible with ERPNext (GPL-3.0) and
Frappe (MIT). The owner may choose differently before a first release.
**Inventory.** Each image's SBOM comes with a licence inventory (`scripts/ci/licences.py`,
committed copy [ops/security/licences.md](ops/security/licences.md)): no network-copyleft
(AGPL) component; strong copyleft is ERPNext, the custom apps and the Debian base system
(source available from Debian, and to be offered with the image if it is given to anyone
outside the organisation); the packages whose licence the SBOM does not identify, mostly X.Org
libraries with unrecognised licence texts, are listed there for the owner's review.

## D-026 Container and browser tests run on hosted CI runners

**Decision.** The development machine's memory belongs to other work, so everything that
needs containers or a browser runs on GitHub-hosted runners of the public repository: the
integration tests on the pinned bench, images, scan, smoke test, the browser suite and the
API tests (`ci.yml`), the synthetic datasets (`dataset.yml`), load tests (`perf.yml`) and
the production rehearsal (`rehearsal.yml`). Lint and the translation tooling
(`scripts/dev/i18n-host.sh`) run locally without containers.
**Consequences.** Every check is reproducible from a commit by anyone; feedback takes a CI
run. Main's runs queue instead of cancelling each other, so each commit keeps its result.

## D-027 Long dataset runs as chained, resumable segments

**Decision.** A hosted job may run for six hours; the standard profile takes longer. The
dataset workflow chains segments: each restores the previous export, generates for a time
budget, stops at the end of a simulated day through the Seed Run's stop request (the runner
resumes from the last committed day) and exports again; the last one validates.
**Consequences.** The standard dataset is an artifact of a recorded run, with its counts
and validation, and the performance tests load it from there. On a 4-vCPU hosted runner the
first standard run simulated about a month in its first 4-hour segment (the catalogue and
master data included): the workflow chains up to 18 segments, and a run that stopped is
continued by a new dispatch with `resume_run` (its first segment starts from that run's last
export) instead of starting again.

## D-028 Forward and reserve storage, replenished on on-hand stock

**Decision.** Put-away uses ERPNext Putaway Rules (forward store up to a part's capacity,
then reserve storage with unlimited capacity, because ERPNext drops a received quantity
that fits no rule). Forward-store reorder rules are ERPNext Item Reorder rows of type
Transfer, but the daily replenishment compares on-hand stock: ERPNext's own reorder job
uses projected quantity, which counts open purchase orders into the store and would leave
the forward store empty while the stock sits in reserve. Auto indent stays off.
**Consequences.** Every move is a standard document (Material Request, Pick List, Stock
Entry); MRP nets reserve storage like any other plant warehouse.

## D-029 Read implies print

**Decision.** The schema DSL grants print with read (permission level 0). A print format
shows only fields the role can read, and the desk offers Print only with the print right.
**Consequences.** Every role that can open a document can print it; e-mail and export stay
explicit rights.

## D-030 A small runtime image, and accepted findings that need an owner decision

**Decision.** The runtime image keeps only the realtime server's Node packages (the direct
dependencies keep their licence files for Frappe's attribution page) and no package
installer. The image scan fails on any fixable critical finding not accepted in
`config/security/image-allowlist.txt` by package and installed version, with a finding ID of
`ops/security/findings.md`; a release build also needs the owner's decision on each.
**Consequences.** Upstream-pinned findings (Pillow) do not block CI, but do block a release
until the owner accepts them; an upgrade of the package ends the acceptance by itself.

## D-031 The permission design is a checked table

**Decision.** `ops/security/permission-matrix.md` documents each persona's rights on the key
DocTypes, and an integration test fails on any difference from the live permission tables.
**Consequences.** A role change is a reviewed change of the documented matrix.

## D-032 Point-in-time recovery from shipped binary logs

**Decision.** Every 5 minutes the `cron` service closes MariaDB's binary log, copies the
closed logs into a mirror and stores it and the site's files in restic. The nightly dump
records its own binary log position (`mariadb-dump --master-data=2` through a wrapper on
`PATH`: with `--single-transaction` MariaDB takes it from the snapshot, without locks or extra
privileges). A restore replays the logs from that position, filtered on the source database
and renamed to the target (MariaDB filters before `--rewrite-db`, so the filter names the
source), then copies the newest files.
**Alternatives.** A replica on another host (the high-availability option; operator
infrastructure); replaying from a time instead of a position (re-applies transactions the dump
already holds, and duplicates rows of tables without a unique key such as `tabSingles`).
**Consequences.** The recovery point is about 5 minutes; every drill measures it. Only the
last three nightly sets can be brought forward from the newest binary log snapshot.

## D-033 InnoDB snapshot isolation off

**Decision.** MariaDB runs with `--innodb-snapshot-isolation=OFF` (production and development
compose). ERPNext's stock ledger and Frappe's document saves rely on locking reads that wait
for a concurrent transaction and then see its committed row; MariaDB 11.6+ (snapshot isolation
on by default) fails such a read with error 1020 instead, which Frappe reports as a deadlock.
**Alternatives.** Keep the default and retry failing requests (users and jobs would see
intermittent errors on concurrent stock postings); run ERPNext at READ COMMITTED (not how it
is tested).
**Consequences.** The InnoDB behaviour of MariaDB up to 11.5 and of MySQL, under which ERPNext
is developed. A concurrent duplicate insert still waits and then fails with a duplicate key,
which the integration hub and the idempotency keys handle.


## D-034 Versions of the custom methods' contract, and per-user request limits

**Decision.** The OpenAPI contract of the custom methods (`integration_hub/openapi.json`) has a
semantic version (`info.version`). A method path keeps its behaviour within a major version:
an addition (a method, an optional parameter, a result property, a documented answer) raises
the minor version; a breaking change (a method or verb removed, a parameter removed, retyped or
newly required, a result property removed or retyped, guest access withdrawn) needs a new
major version. To give partners time, the new behaviour comes as a new path (a new function,
or a module's `v2` package) while the old path stays, marked deprecated (a `Deprecated:` line
in its docstring), for at least one release; removing it then raises the major version.
Wording changes need no new version. CI compares the generated contract with the one before
the change (`openapi.py --compat`) and fails when the version does not follow. Changes of the
roles a method requires are access decisions (access review), not contract versions.
Separately, the portal and shop-floor methods count against per-user limits per minute
(Automotive Settings, Request Limits): portal reads 300, portal changes 60 (including partner
uploads), shop floor 240; over the limit the request is refused with HTTP 429 before the
method runs.
**Alternatives.** Version prefixes in every path from the start (`/api/v1/...` of our own:
Frappe routes methods by module path, so this would be every module twice); Frappe's
`rate_limit` decorator (counts per IP address: partners behind one corporate proxy, or the
tablets of a line, would share one budget).
**Consequences.** Partners and integrators can rely on a path for a major version and see
additions in the contract's minor versions. A method whose shape must change keeps a
deprecated twin for a release. The limits are site settings an IT operator can raise; the
429s show in the 4xx responses of the metrics.

## D-035 An employee's personal details are HR's; every export is logged

**Decision.** The personal fields of ERPNext's Employee (date of birth, gender, private contact
details, emergency contact, identity documents, health, family, education, work history, pay,
bank details, terms of employment, exit) are at permission level 1, granted to ERPNext's HR User
and HR Manager roles only; no persona includes them, System Manager neither. Report downloads
are logged in the Access Log through a replacement of Frappe's download method
(`override_whitelisted_methods`), as Frappe already logs the report view and Data Export.
**Alternatives.** A User Permission on each user's own Employee record (ERPNext's
`create_user_permission`): it would also hide the employees that job cards and plant
assignments name, and ERPNext adds a company permission with it (D-007); removing the
"Employee" role ERPNext grants on every link (it re-adds it on each save of the Employee).
**Consequences.** Colleagues read the directory fields of all employees; nobody but HR the
rest, including the employee's own record. A deployment without HR staff in the ERP keeps the
personal details empty or in its HR system. Each update of the Frappe pin is checked for a new
download path in the desk that would bypass the replacement (upgrade.md).
