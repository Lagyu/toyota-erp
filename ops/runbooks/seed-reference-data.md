# Synthetic reference data (reference and staging sites only)

Loading, resuming and validating the deterministic synthetic Hinode dataset. The generator is the `automotive_seed` app, driven by `apps/automotive_seed/automotive_seed/runner.py` and the profiles in `apps/automotive_seed/automotive_seed/profiles/` (also linked from `seed/profiles`).

> **Never run this on production.** Production images do not contain `automotive_seed`: `scripts/image/build.sh` adds it only with `--reference`. The runner also refuses to run unless site config has `automotive_allow_seed: 1`, and refuses on any site whose `automotive_environment` is `production`.

## Prerequisites

- **A reference image:** `./scripts/image/build.sh --reference`, which produces tags ending in `-reference`. See [upgrade.md](upgrade.md#build-the-image).
- **A reference stack** deployed from `config/env/reference.env.example` (`ENVIRONMENT=reference`) as in [install-fresh-host.md](install-fresh-host.md). With `ENVIRONMENT=reference`, `scripts/deploy/new-site.sh`:
  - installs `automotive_seed`;
  - sets `automotive_allow_seed` to 1;
  - sets `automotive_environment` to `reference`.
- **A staging site** built from a `-reference` image is *not* enabled by `new-site.sh`. To seed it, run the same two steps by hand:

  ```bash
  ./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" install-app automotive_seed
  ./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" set-config -p automotive_allow_seed 1
  ```

- **Disk space and time for the profile** (next section). Seed data lands in the site database and the sites volume.

## Profiles

All profiles use seed `20261024`: the same seed and profile always produce the same documents in the same order.

| | `small` | `standard` | `large` |
|---|---|---|---|
| Purpose | CI and development (end-to-end suite) | principal acceptance dataset (brief §5) | sizing and load tests |
| Simulated history | from 2026-09-01, 0.5 month | from 2025-10-01, 12 months | from 2025-10-01, 12 months |
| Target internal / partner users | 40 / 20 | 120 / 80 | 120 / 80 |
| Target suppliers / dealers | 15 / 6 | 60 / 20 | 60 / 20 |
| Target items | 300 | 8,000 | 16,000 |
| Target BOMs | 60 | 800 | 800 |
| Target work orders | 120 | 12,000 | 24,000 |
| Target stock-ledger events | 600 | 80,000 | 160,000 |
| Aichi vehicles per day | 6 | 30 | 60 |
| Dealer order lines per day | 6 | 26 | 52 |
| Dealers (JP + US) | 4 + 2 | 12 + 8 | 12 + 8 |
| EDI suppliers / dealers | 3 / 1 | 12 / 4 | 12 / 4 |

The `targets` of a profile are minimums checked by the validation stage.

**Timing:**

- `small`: about **35 minutes** on the development host (15 simulated days).
- `standard`: **not measured yet**.
- `large`: **not measured yet**.

Record your own timings and database size after a run (Seed Run *Started At* / *Finished At*).

## How a run works

- **Run record.** A run is the **Seed Run** document `SEED-<profile>-<seed>` (`/desk/seed-run`), for example `SEED-standard-20261024`.
- **Stages, in order:** `tenant`, `people`, `catalog`, `suppliers`, `dealers`, `bank_accounts`, `sourcing`, `kanban`, `pricing`, `edi_partners`, `opening`, `history`, `validation`. Each finished stage is marked Done in the *Stages* table, with a note, its duration and a document count.
- **History** simulates one day at a time, and each day is **one database transaction**. *Simulated Date* shows the last committed day.
- **Resuming.** An interruption (container restart, crash) costs at most the day in flight. A rerun skips Done stages and continues from the day after *Simulated Date*, with the same result as an uninterrupted run.
- **State file.** Generator state between stages is kept in `sites/<site>/private/files/seed/<run>-state.json`, so site backups (and datasets restored from them) carry it. A file left at the older location `private/seed/` is moved there on the next run.
- **Background jobs** run inline during generation, and emails are muted.
- **Documents.** Everything is created through ERPNext and application documents with their validations; nothing is written into ledgers directly.

## Run the generator on a deployed reference stack

The run takes long, so start it detached inside the backend container and log to a file:

```bash
ENV=/etc/hinode-reference/reference.env        # your copy of reference.env.example
SITE=erp-reference.hinode.example
./scripts/deploy/compose.sh "$ENV" exec -d -e PYTHONHASHSEED=0 backend bash -c \
  "bench --site $SITE execute automotive_seed.runner.run --kwargs \"{'profile': 'standard'}\" >>logs/seed-standard.log 2>&1"
```

`PYTHONHASHSEED=0` is required (the run refuses to start without it): Python orders sets of
strings by their hashes, which it randomises per process unless the seed is fixed, and a
resumed run is a new process. With it, the same profile and seed give the same documents.

Follow progress:

- Seed Run `SEED-standard-20261024` → *Status* (Running), *Stage*, *Simulated Date*, *Stages* table.
- `./scripts/deploy/compose.sh "$ENV" exec -T backend tail -n 20 logs/seed-standard.log`. The log is inside the backend container; it is lost if the container is recreated, while the Seed Run is not.

A detached process stops when the backend container is restarted or recreated, for example by an upgrade or `up -d` with changed settings. Restart it with the same command: it resumes.

### Pause and resume

- **Stop at a date:** add `'until': 'YYYY-MM-DD'`, for example `--kwargs "{'profile': 'standard', 'until': '2026-01-31'}"`. The run stops after that simulated day with status **Paused**.
- **Stop as soon as possible:**

  ```bash
  ./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" execute \
    automotive_seed.runner.request_stop --kwargs "{'run': 'SEED-standard-20261024'}"
  ```

  This sets *Stop Requested*, and the history pauses after the current day with status **Paused**.
- **Resume:** run the same `automotive_seed.runner.run` command again, without `until` to finish.

A run that has not reached the validation stage always ends as **Paused**, whether stopped by a date or a request. *Finished At* is set only when the run completes.

**Interrupted runs.** A run killed at any moment (a crash, a container restart, a CI segment out
of time) resumes at the end of the last whole simulated day: each day's documents, the date it
reached and the simulation's own state commit together, and a setup stage interrupted half way
is run again. The validation records a **fingerprint** of the generated documents (per DocType,
fields the profile and seed decide: names from naming series, simulated dates, quantities and
amounts). Two runs of one profile and seed on the same code give the same fingerprint, however
often they were interrupted: `dataset.yml` with `minutes` small and `grace_minutes: 0` stops a
run in the middle of days, and `dataset-compare.yml` compares two runs' fingerprints.

### Failure

If a stage raises, the transaction of the day in flight is rolled back. The Seed Run gets status **Failed** with the traceback in *Error*, and the command prints the exception. Fix the cause (often configuration or capacity, e.g. disk or database limits), then rerun to resume.

## Validation

The last stage checks the invariants on the ledgers and stores the result in the Seed Run *Validation* field (JSON). The status becomes **Validated** or **Validation Failed**. Checks:

- trial balance per company;
- stock value = stock accounts per company;
- bin quantity = stock ledger balance;
- no negative stock;
- receivables and payables GL = payment ledger;
- intercompany per company pair;
- every built vehicle has genealogy;
- vehicle location = serial warehouse;
- no unreleased vehicle delivered;
- every standard purchase order approved;
- every supplier payment dual-approved;
- no self-approval;
- no submitted invoice with open match exceptions;
- audit hash chain intact;
- inbound messages exactly once;
- `volume <target> >= <minimum>` for every profile target.

Re-run the checks later, for example after migrations or manual changes on the reference site:

```bash
./scripts/deploy/compose.sh "$ENV" exec -T backend bench --site "$SITE" execute \
  automotive_seed.runner.revalidate --kwargs "{'profile': 'standard'}"
# prints {"ok": true|false, "failed": [names of failed checks]} and updates the Seed Run
```

**A run is complete when:**

- every stage in *Stages* is Done;
- *Simulated Date* equals *End Date*;
- *Status* is Validated.

**Status values:** Queued, Running, Paused, Failed, Validated, Validation Failed.

## Development bench (scripts/seed/)

These scripts run **inside the development bench container** (`scripts/dev/compose.sh`, see [testing.md](testing.md)):

| Script | Purpose |
|---|---|
| `scripts/seed/create-site.sh SITE` | Create a reference site with all apps, `automotive_allow_seed` 1 and `automotive_environment` reference (idempotent) |
| `scripts/seed/reset-site.sh SITE [--rebuild]` | Put the site back to "apps installed, no data". The first call builds it and keeps a snapshot in `/home/frappe/dev/seed-base/`; later calls restore the snapshot and migrate. Also removes the seed state (`private/files/seed`, and the older `private/seed`) and `private/integration`. |
| `scripts/seed/run.sh SITE PROFILE [UNTIL_DATE]` | Generate or resume a profile (`automotive_seed.runner.run`) |

```bash
./scripts/dev/compose.sh exec -T bench bash -lc '/workspace/scripts/seed/reset-site.sh small.localhost'
./scripts/dev/compose.sh exec -d bench bash -lc \
  '/workspace/scripts/seed/run.sh small.localhost small >>/home/frappe/dev/logs/seed-small.localhost.log 2>&1'
```

`/home/frappe/dev/logs/` in the container is `.local/dev-state/logs/` on the host.

## End-to-end fixtures

Used by `scripts/ci/e2e.sh` on reference sites only. Both functions call the same guard as the runner and require System Manager.

- **`automotive_seed.e2e.prepare_users`** picks one dataset user per persona and sets a **password for this run only**. It writes them to the given output path with mode 0600. There are 22 personas, from executive, controller and buyer through the Aichi plant roles to the NA warehouse and Kyushu logistics.
- **`automotive_seed.e2e.prepare_journeys`** arranges journey preconditions through the dataset's own flows, for example material at the line for a build to order, or an EDI partner's signing credentials.

```bash
bench --site SITE execute automotive_seed.e2e.prepare_users --kwargs "{'output': '/tmp/credentials.json'}"
bench --site SITE execute automotive_seed.e2e.prepare_journeys --kwargs "{'output': '/tmp/fixtures.json'}"
```

`prepare_users` changes real passwords of dataset users. Run it only where those accounts are test accounts. `e2e.sh` keeps the files in `.local/e2e/credentials.json` and `.local/e2e/fixtures.json` (0600).

## Starting over on a deployed reference stack

There is no reset script for deployed stacks. To start from an empty site, destroy the stack's volumes and recreate the site. This deletes **all data of that stack**, including its local backups:

```bash
./scripts/deploy/compose.sh "$ENV" down --volumes --remove-orphans
./scripts/deploy/compose.sh "$ENV" up -d
./scripts/deploy/new-site.sh "$ENV"
```

## Related

- [testing.md](testing.md): the e2e suite that uses the small dataset
- [install-fresh-host.md](install-fresh-host.md)
- `seed/README.md`
