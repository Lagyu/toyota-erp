# Performance tests

Requirements: PRF-2 (50 concurrent users plus background jobs on the standard dataset),
PRF-3 (a 150-user peak; p95 of common list and detail responses at most 2 s, of ordinary
form submissions at most 4 s, no unbounded growth of failed jobs), TST-9 and GATE-7
(measured results under declared hardware and dataset conditions).

## What runs

- **Dataset:** the standard profile's export from the `dataset` workflow (12 simulated
  months, validated invariants), restored on a reference stack built from the commit
  under test and migrated to it.
- **Stack:** the production compose (`scripts/deploy/compose.sh`) from the reference image,
  TLS off, gunicorn 4 workers × 4 threads, two queue workers, the scheduler running.
- **Load:** `perf/locustfile.py` (Locust, pinned in `perf/requirements.txt`):
  - desk users in the dataset's head-count mix of personas (warehouse, planning,
    purchasing, shop floor, quality, finance, sales, management, IT), each signed in as a
    dataset user. About 80 % of their actions read (list views with their counts, forms
    opened from a list, workspaces with their number cards, reports); 20 % write (ToDos,
    comments, draft material requests, 1-unit stock transfers that the next one reverses);
  - supplier and dealer portal users, limited to their portal role's functions. One account
    per portal persona is shared by all its virtual users (real partners each have their
    own), so the run lifts the per-user request limits of the portals and the shop floor
    (Automotive Settings, *Request Limits*), which would otherwise throttle the shared account;
  - one background planner starting an MRP planning run every five minutes and waiting
    for it (`job:Planning Run`: completion time, no target).
  Think time 5–15 s between actions.
- **Phases:** `steady:50:20m` then `peak:150:10m` on the same stack (each starts its users
  over about ten seconds).
- **Measures:** client-side percentiles per request group (`read:*` GET, `write:*` other
  methods) and per operation, failures, errors inside the test; server-side, the
  `erp_http_request_duration_seconds` histogram and the failed-job count from the metrics
  endpoint, before and after each phase.

## How to run

```bash
gh workflow run dataset.yml -f profile=standard     # once per dataset change (several hours)
gh workflow run perf.yml -f profile=standard        # latest successful dataset run
gh workflow run perf.yml -f dataset_run=<run id> -f phases="steady:50:20m"
```

Results: the run's summary page, and the `perf-results` artifact (per phase: `summary.txt`,
`summary.json`, Locust's CSV/HTML/JSON, metrics snapshots, `run.json` with the hardware;
the stack's container logs). `scripts/perf/run.sh IMAGE DATASET_DIR PHASE...` runs the same
on any Docker host; never against production (the write scenarios create documents, and
the persona passwords are generated for the run).

## Declared conditions

The hosted runner is the measured environment: the stack and the load generator share
one machine (`run.json` records its vCPUs, CPU model and memory; GitHub's standard Linux
runner has 4 vCPUs and 16 GB). That is well below the brief's initial reference sizing (16 vCPU,
64 GB, dedicated database storage), and the load generator takes part of the CPU, so the
results are conservative. Re-run `scripts/perf/run.sh` on the target hardware before go-live.

## Results

Pending: the first measurement runs on the first validated standard dataset.
