---
audience: developer
document_type: report
status: current
---

# Stage 2, 3 and 5 performance verification

The later [complete-page consolidation](2026-11-05-page-open-consolidation.md)
reduces physical opens for saved pages. This report records the preceding
SQL and action-path changes and their first live check.

The measured browser actions now reuse local DuckDB owners and successful
structural validation within each synchronous operation. Each access still
checks its requested lineage. Remote reads and background job dispatch happen
after the local owners close. Authorization, evidence hashes, transaction
boundaries, formula restrictions, saved decisions and recovery checks remain
in force.

## Measurement scope

The comparison uses fictional workspaces on this Mac, Python 3.12.14 and the
same pinned dependency environment. Each route warms once and records three
requests for every combination of Odoo 19 or 20 and local or remote access.
Before and after cases alternate to reduce host-load bias. The table reports
the median of the four case medians. Ranges show differences between cases.
CPU time and every raw sample remain in the JSON evidence.

The before snapshot preserves the checkout's existing uncommitted work. The
after snapshot adds this performance change and excludes the duplicate-row
dialog that another chat was implementing concurrently. Both snapshots use
an identical query probe and fixtures. The final shared checkout receives
separate route regressions and repository checks. No running application
store was opened by these benchmarks, and these fixture benchmarks did not restart the active server. The later
live check is recorded below.

The supporting Odoo reader is a deterministic fictional boundary. These
measurements establish local request cost, not remote network latency or a
live-production speedup. Preparation intake measures validation and dispatch
preparation; its job manager is replaced at the dispatch boundary so it does
not time process startup or preparation execution.

## Paired results

| Action | Physical opens, before → after | SQL statements, before → after | Elapsed milliseconds, before → after |
| --- | ---: | ---: | ---: |
| Stage 2: open saved Odoo details | 6 → 6 | 112–114 → 68–70 | 186 → 176 |
| Stage 3: open Match data | 8 → 8 | 209–211 → 125–127 | 317 → 261 |
| Stage 5: open an unprepared summary | 6 → 6 | 130–132 → 78–80 | 221 → 177 |
| Stage 3: validate a formula | 11 → 6 | 41 → 33 | 219 → 134 |
| Stage 2: confirm matching keys | 51 → 6 | 203 → 98 | 1114 → 172 |
| Stage 3: recover a rejected table order | 67–69 → 6 | 264–266 → 122–124 | 1444 → 194 |
| Stage 5: open a prepared summary | 6 → 6 | 321–323 → 161–163 | 359 → 272 |
| Stage 5: open problem groups | 50–52 → 6 | 189–191 → 94–96 | 1198 → 201 |
| Stage 5: preview all problem groups | 6 → 6 | 306–308 → 162–164 | 388 → 250 |
| Stage 5: start preparation intake | 29 → 8 | 120 → 79 | 635 → 254 |

The query probe preserves the retained cursor's owner identity. Otherwise the
probe itself would disable validation reuse and introduce repeated schema
inspection. The new regression budgets check physical opens, SQL statements,
column-catalogue reads, schema checks, business outcomes and lock retries.

The schema-check counter now includes every store contract checked, including
linkage and Data version stores. Older values counted only Workspace engine
checks. Compare catalogue reads when assessing structural work across these
two builds; an increase in the expanded counter does not mean more schema work.

- [Raw before measurements](evidence/2026-11-05-stage235-before.json).
- [Raw after measurements](evidence/2026-11-05-stage235-after.json).
- [Reproducible benchmark](../../scripts/benchmark_stage235.py).

Run the benchmark from the checkout root with its pinned runner:

```sh
UV_CACHE_DIR=.uv-cache .tools/uv/uv run --frozen --extra test --group quality python scripts/benchmark_stage235.py --samples 3 --output /tmp/impodo-stage235.json
```

## Implementation and safeguards

The shared [schema boundary](../../src/impodo/adapters/duckdb/schema/forward_upgrades.py)
reuses only successful structural validation for the same retained database,
configuration, generation and schema version. Requested Workspace and Data
version identities are still checked on every access. Different stores,
failed checks and the next operation validate independently. No connection or
schema marker survives across requests.

The [Odoo details routes](../../src/impodo/web/routers/schema.py) keep model
selection and key confirmation in bounded local commands. Metadata discovery
and capture release owners before Odoo calls and revalidate during publication.
The [mapping routes](../../src/impodo/web/routers/mapping.py) bound formula
validation, table ordering, checked-row rendering, live-check preparation,
local acknowledgments and recovery reads. The
[preparation intake](../../src/impodo/web/run_commands.py) closes its local
scope before enqueue or retry. The
[problem-group routes](../../src/impodo/web/routers/preflight.py) verify and
render inside one bounded page scope, including rejected acceptance recovery.

Large problem-group previews still restore and verify complete comparison
snapshots and approved preparation evidence. A separate 5,000-product profile
identified snapshot restoration and portable-evidence validation as significant
remaining CPU work. This change preserves those checks. The small-fixture
latency results must not be extrapolated to large previews without measuring
their row count and evidence size.

## Verification

- The isolated candidate passed the complete repository gate: Ruff,
  Basedpyright, compilation, whitespace, 88 architecture tests, documentation
  checks and code-documentation checks.
- The focused browser, store, worker-lifetime, upgrade and Recipe-recovery run
  passed 86 tests and 66 subtests. Additional capture-boundary checks passed.
- The portable schema-metric sweep exercised 57 test functions. Older
  engine-only assertions were corrected for complete store coverage. The
  sweep passed 53 cases; its remaining three affected test functions passed
  on a focused rerun, including all four company-access subcases. Physical-open
  ceilings and business assertions were preserved. Two opt-in scale tests were
  skipped.
- Four Recipe-reuse tests passed for Odoo 19 and 20 with local and remote
  access. The new stage regressions cover all four combinations as well.
- The final shared checkout passed 12 route, store and capture-boundary tests
  with 22 subtests. The included-parent browser suite passed two further tests
  and all four version/access subcases. The shared checkout also passed the
  complete repository gate.
- Both repository-local performance and Python quality skills were updated
  and validated. Repository checks do not depend on local skill files.

The Linux/PostgreSQL live suite is not qualified on this Mac. An initial broad
metric selection included one such test; it stopped at setup because
`IMPODO_TEST_POSTGRES_DSN` was absent. The corrected selection excludes
host-specific suites. The later live check covers saved page reads on the current target. It does
not qualify the Linux/PostgreSQL suite or live Odoo operations.


## Live saved-workspace check

The user approved the restart on 5 November. At inspection, both earlier
servers had already stopped normally. The existing browser tab pointed to the
latest server's Quit page. Impodo was restored at the same loopback port
without opening or navigating a browser tab. The stopped launcher's in-memory
session secret was unavailable, so the restored process requires a normal
one-time sign-in.

The live check used the application's normal launch-token authentication and
read-only HTTP requests against the saved workspace. Its saved target reports
Odoo 20.0. Each stage warmed once, followed by seven interleaved measurements.
All 24 requests returned HTTP 200 without redirects. Mapping diagnostics
reported working draft version 9 throughout. All 116 saved files, totalling
923,132,799 bytes, had identical SHA-256 fingerprints before and after the
measurements. No save, confirmation, preparation, comparison or load command
was executed.

| Saved page | Physical opens per request | Complete store checks | Median server time | Observed range | Median connection time |
| --- | ---: | ---: | ---: | ---: | ---: |
| Stage 2: Odoo details | 6 | 4 | 133.6 ms | 133.5–134.8 ms | 86.5 ms |
| Stage 3: Match data | 8 | 6 | 176.4 ms | 175.1–177.8 ms | 99.1 ms |
| Stage 5: summary | 7 | 4 | 171.6 ms | 169.8–173.1 ms | 120.8 ms |

Every measured request had zero lock retries. Median structural-validation
times were 13.5, 20.9 and 13.7 milliseconds respectively. Template-render
medians were 3.8, 4.1 and 0.7 milliseconds. Connection setup remains the
largest measured component. Those timings overlap higher-level page timings
and must not be added together.

Earlier diagnostics from the previous launcher recorded stage medians of
139.7 milliseconds for Odoo details, 205.5 milliseconds for Match data, and
363.9 milliseconds for the summary. They recorded the same respective physical
open counts of 6, 8 and 7. These are historical context: the log does not bind
a request to a workspace or evidence fingerprint, and the samples differ in
page state, cache warmth and host load. They do not establish a controlled
live speedup. The paired fixture table above remains the before/after evidence
for SQL reductions and action performance.

Production diagnostics measure physical opens, store validations and timings;
they do not count SQL statements. Live verification here covers saved page
reads on the current Odoo 20 target. Both supported Odoo majors and local and
remote action boundaries remain covered by the isolated regression matrix.
Live mutation paths and Odoo network latency were omitted to preserve saved
work. Browser rendering and interaction were not measured because the browser
connector did not expose the existing tab.

The measurement session ended through the normal Quit action. The final
server was restored at the same port with protected storage, loopback checks
and normal authentication enabled. A fresh one-time launch link was reserved
for the user's existing tab. The final launch did not open a browser.

- [Sanitized live diagnostics and integrity result](evidence/2026-11-05-stage235-live.json).
