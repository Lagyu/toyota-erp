# Synthetic data profiles

The deterministic synthetic data generator is the `automotive_seed` Frappe app
([`apps/automotive_seed/`](../apps/automotive_seed/)). Its profiles live inside the app so
that reference images carry them; [`profiles/`](profiles/) here links to them.

| Profile | Purpose | History | Volume drivers |
|---|---|---|---|
| [`small`](profiles/small.yaml) | CI and development dataset (end-to-end suite) | 2026-09-01, half a month | 6 vehicles/day, small catalogue |
| [`standard`](profiles/standard.yaml) | acceptance dataset of requirement §5 | 2025-10-01, 12 months | 30 vehicles/day, ≥8,000 items, ≥12,000 work orders, ≥80,000 stock ledger events, 60 suppliers, 20 dealers, ≥120 internal / ≥80 partner users |
| [`large`](profiles/large.yaml) | sizing and load tests | 2025-10-01, 12 months | twice the standard volumes |
| [`closing`](profiles/closing.yaml) | the month-end and fiscal-year-end flows (revaluation, bank statements, Period Closing Vouchers) at small volumes | 2026-03-16, three weeks | the small profile's volumes |

All profiles use seed `20261024`: the same seed and profile always produce the same
documents in the same order. The generator works through the same documents and APIs as
the application's users — each step is posted by the dataset person who owns it, under a
simulated clock that keeps every day's postings in order — and validates financial and
stock invariants at the end (`Seed Run` → validation).

Run it only on reference or staging sites; it refuses to run unless the site enables
`automotive_allow_seed`. How to create, run, resume and validate a dataset:
[ops/runbooks/seed-reference-data.md](../ops/runbooks/seed-reference-data.md).
