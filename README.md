# Toyota ERP Clone — Hinode Automotive Group ERP

A self-hosted, multi-company ERP for an automotive manufacturer, built on
**ERPNext v16 / Frappe Framework v16** as a set of custom Frappe applications. It runs the
business of a fictional group, **Hinode Automotive Group**: vehicle assembly in Aichi
(Japan), powertrain manufacturing in Kyushu (Japan) and vehicle distribution in North
America, with their suppliers and dealers working in partner portals.

"Toyota ERP Clone" is the project's working name only. The product shows no Toyota
names, logos or data; every record is synthetic (see [Identity and data](#identity-and-data)).

## What it does

| Area | Highlights |
|---|---|
| Enterprise setup (ERP-01) | Three companies, plants, production lines and stations, ≥15 warehouses, fiscal calendars, master-data duplicate checks, tenant configuration as code |
| Product engineering (ERP-02) | Vehicle models with option rules, configurator, BOM revisions with effectivity, ECR → ECO with impact assessment and approval, where-used |
| Planning (ERP-03) | Versioned planning parameter sets, deterministic MRP with shortage and capacity exceptions, replay verification, release to production plans; leveled daily line sequence |
| Manufacturing execution (ERP-04) | VIN-level Vehicle Units, MES reporting with idempotency keys, Kanban loops/cards/signals, andon with jidoka holds, shift handover, WIP/OEE/variance reports |
| Procurement (ERP-05) | Supplier onboarding, approved supplier list, sourcing decisions, approval matrices, blanket orders and call-offs, delivery schedules, acknowledgements, ASNs, three-way match with exception workflow |
| Inventory and logistics (ERP-06) | Receiving from ASNs, discrepancies, cycle counts, consignment stock, transport bookings, shipment manifests with proof of delivery |
| Quality and traceability (ERP-07) | Inspections, holds enforced across stock and shipping documents, nonconformance with CAPA, dispositions, multi-level genealogy, recall analysis |
| Dealer operations (ERP-08) | Dealer registration, portal ordering with configuration and promise dates, build-to-order and FIFO allocation with serial reservations, change/cancel requests, delivery acceptance, warranty start |
| Finance (ERP-09) | Standard ERPNext ledgers; intercompany transfer pricing and hand-offs, group consolidation view, period close checklist with waivers and approval, verified bank accounts, dual-approved payments |
| People and roles (ERP-10) | 16 internal personas and two partner types as least-privilege role profiles, company/plant/partner row scoping, approvals with maker/checker and delegation, segregation-of-duties rules, hash-chained audit trail |
| Integrations (ERP-11) | Signed HTTPS inbound endpoint, idempotent inbox, durable outbox with retries and dead-lettering, Hinode EDI format, file-drop adapter for partners without live connectivity |
| Experience and MI (ERP-12) | Role workspaces with KPI cards, supplier and dealer portals (desktop and tablet), approval inbox, management reports, KPI catalogue with formulas and reconciliation checks |

The ten mandatory business journeys (J-01 … J-10) run end to end through the real
application, in the browser, as the people who do each step — see [`e2e/`](e2e/) and
[REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md).

## Repository layout

```text
apps/                 custom Frappe apps
  automotive_core/            governance, approvals, audit, integration hub, finance controls,
                              analytics/KPIs, operations endpoints, tenant setup
  automotive_manufacturing/   engineering, planning/MRP, MES, shop floor, quality, traceability
  automotive_supply_chain/    suppliers, procurement controls, logistics, dealers, intercompany
  automotive_portals/         supplier and dealer portals
  automotive_seed/            deterministic synthetic data generator (never in production images)
  */schema/                   DocType specifications (JSON is generated from these)
e2e/                  Playwright business acceptance suite (journeys, roles, portals, shop floor,
                      navigation, accessibility, Japanese UI)
integration_tests/    API contract tests: the running API against the committed OpenAPI document
infra/                pinned versions, image build, production compose overrides, monitoring
scripts/              development, CI, image, deployment, seed and host scripts
ops/                  backup and restore drill scripts, operations runbooks, security findings
config/               safe configuration templates: environment files, security review allowlists, Japanese glossary
seed/                 pointer to the synthetic data profiles
.github/workflows/    CI
```

## Prerequisites

- Linux with Docker Engine and Compose v2 (developed and tested on Ubuntu 24.04+).
  `scripts/host/install-docker-ubuntu.sh` installs Docker inside a resource-capped systemd
  slice.
- Development: 4 vCPU, 16 GB RAM, 40 GB free disk. Production reference sizing:
  16 vCPU, 64 GB RAM, NVMe storage (requirement brief §7).
- Outbound access to GitHub and the container registries during builds (all versions are
  pinned in [`infra/versions.env`](infra/versions.env)).

## Quick start (development)

```bash
./scripts/dev/init-env.sh        # generate .local/dev.env with secrets (once)
./scripts/dev/up.sh              # start MariaDB, Redis, bench, worker; bootstrap runs detached
tail -f .local/dev-state/logs/bootstrap.log   # until "BOOTSTRAP OK"
```

The bootstrap installs Frappe and ERPNext at the pinned commits, links the custom apps and
creates a development site and a test site. Then:

```bash
./scripts/ci/lint.sh                         # ruff (lint, format), shellcheck, schema drift, translations, YAML/JSON, identity
./scripts/dev/test-all.sh                    # integration tests of every app on test.localhost
./scripts/dev/compose.sh exec -T bench bash -lc '/workspace/scripts/seed/reset-site.sh small.localhost'
./scripts/dev/compose.sh exec -d bench bash -lc '/workspace/scripts/seed/run.sh small.localhost small'
```

The small profile (15 simulated days) takes about 35 minutes; progress is in the
`Seed Run` document of the site and in `.local/dev-state/logs/seed-small.localhost.log`
(re-running `run.sh` resumes from the last completed day). Serve the site and run the
browser suite:

```bash
./scripts/dev/serve.sh            # every dev site on port 18000, by host name
./scripts/ci/e2e.sh --url http://small.localhost:18000 --site small.localhost
./scripts/ci/contract.sh --url http://127.0.0.1:18000 --site small.localhost   # API vs OpenAPI
```

Every persona of the dataset can sign in with a password generated for the run (kept in
`.local/e2e/credentials.json`, mode 0600).

## Production

Production uses the official `frappe_docker` compose files plus the overrides in
[`infra/deploy/`](infra/deploy/) and an immutable image built from the pins:

```bash
./scripts/image/build.sh                                  # toyota-erp/erp:<version>-<git sha>
cp config/env/production.env.example /etc/hinode/production.env   # edit, provision secrets
./scripts/deploy/compose.sh /etc/hinode/production.env up -d
./scripts/deploy/new-site.sh /etc/hinode/production.env
./scripts/deploy/smoke.sh /etc/hinode/production.env
```

Installation, upgrades and rollback, backups and restore drills, monitoring, security
operations, access reviews and troubleshooting are covered in [`ops/`](ops/README.md).
`./scripts/ci/smoke.sh IMAGE` rehearses the whole cycle (fresh stack, site, health and
portal checks, encrypted backup, restore drill) on one host.

## Testing

| Suite | Command | What it covers |
|---|---|---|
| Lint | `./scripts/ci/lint.sh` | ruff (lint and formatting), shellcheck, generated-schema drift, OpenAPI contract and its version, translations, YAML/JSON syntax, alert rule tests, e2e type check, identity boundary |
| Unit | `./scripts/ci/unit.sh` | pure engines without a database: MRP, line sequencing, option rules, VIN |
| Integration | `./scripts/ci/integration.sh` | every app's tests against real ERPNext, MariaDB and Redis |
| End to end | `./scripts/ci/e2e.sh` | J-01 … J-10, role surfaces, supplier/dealer portals, shop floor (desktop and tablet) |
| Smoke | `./scripts/ci/smoke.sh IMAGE` | production-shaped stack, backup and restore drill |
| Supply chain | `./scripts/ci/audit.sh`, `./scripts/ci/sbom.sh IMAGE` | dependency advisories, SBOM and image scan |

The CI workflow ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same scripts.
Workflows started by hand cover the rest on hosted runners:

| Workflow | What it covers |
|---|---|
| [`rehearsal.yml`](.github/workflows/rehearsal.yml) | install by the runbook, upgrade, backups and drills, rollback (`rehearsal.sh`); the Let's Encrypt mode (`acme-check.sh`); a real reboot and power loss of a VM (`reboot-check.sh`); the high-availability option's application tier (`ha-check.sh`) |
| [`dataset.yml`](.github/workflows/dataset.yml), [`dataset-compare.yml`](.github/workflows/dataset-compare.yml) | a synthetic dataset profile generated in resumable segments and validated; two runs compared document by document |
| [`journeys.yml`](.github/workflows/journeys.yml) | the browser and API suites on a dataset export with the scheduler on |
| [`drill.yml`](.github/workflows/drill.yml) | the backup and the restore drill with point-in-time recovery on a dataset export (sizes, durations, recovery point) |
| [`perf.yml`](.github/workflows/perf.yml) | the load tests on the standard dataset |

Procedures: [ops/runbooks/testing.md](ops/runbooks/testing.md).

## Identity and data

- All people, companies, suppliers, dealers, parts and transactions are generated by
  `automotive_seed` from fixed seeds. No real company, customer or supplier data is used.
- Customer-facing assets (portal pages and scripts, templates, print formats,
  translations, workspaces, tenant configuration and seed data) carry Hinode branding
  only; `scripts/ci/identity_check.py` fails the build if a protected name appears in them.
- The generator ships only in reference images, is installed only on reference sites and
  refuses to run unless the site enables it (`automotive_allow_seed`).

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — boundaries, data flows, security and deployment model
- [DECISIONS.md](DECISIONS.md) — significant technical choices and their rationale
- [REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md) — requirement → implementation → evidence, open items
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — milestones and progress
- [CHANGELOG.md](CHANGELOG.md) — release history
- [ops/README.md](ops/README.md) — operations runbooks

## Status

The system is under active development towards the acceptance gates of the requirement
brief. What is done, partial or still open is tracked item by item in
[REQUIREMENTS_TRACEABILITY.md](REQUIREMENTS_TRACEABILITY.md); nothing there is claimed
without recorded evidence.

## License

GPL-3.0-only (see [LICENSE](LICENSE)), pending the owner's final decision. Upstream:
Frappe Framework (MIT), ERPNext (GPL-3.0), frappe_docker (MIT). Third-party Python and
JavaScript dependencies are listed in the SBOM produced by `scripts/ci/sbom.sh`.
