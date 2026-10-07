---
audience: developer
status: planned
---

# Reduce page-loading time in Source data and Odoo data

## Approved direction and implementation

The approved approach removes repeated database opens, source-package
reconstruction, and structural inspection queries before considering a cache
across requests. Related-table previews reuse one set of source inputs for all
saved rules. The changes preserve the checks that keep source choices and Odoo
details trustworthy.

The user approved implementation on 2026-10-06. The page workers, reuse of
verified source data, batched rule previews, and single capture-plan review are
implemented. Structural checks use bulk column inspection on every check;
validation results are not cached. The remaining optional content and platform
qualification work below is planned.

The next batch, approved on 2026-10-07, also shares the setup selection between
schema routing, Odoo requirements, and recovery. Schema and Odoo source pages
share a safe read-credential status with the dialog when the credential owner
is identical. Active Test recovery counts its required-default blockers in SQL
instead of loading complete Recipe applications and issue records.
The [shared-read report](../testing/stage12-shared-page-reads-2026-10-07.md)
records CPU, memory, database work, and real browser navigation measurements.

The baseline audit used commit `bea06b6380fcb5d3a08d5002af63de7274e30256`.
The intended reader is the developer reviewing the implemented optimization
and choosing any follow-up performance work.

The [implementation report](../testing/stage12-page-loading-2026-10-07.md)
records the changes, checks, measurement method, and qualification limits.

## Scope and measured baseline

In file mode, Stage 1 is **Source data** and Stage 2 is **Odoo data**. In Odoo
source mode, their order becomes **Select data to download**, then **Freeze
Odoo records**. Both variants use the routes examined here.

The audit measured five authenticated GET requests per case using isolated,
fictional test fixtures. The file fixture contains one frozen table with one
row. The Odoo fixture contains one selected model and uses stubbed readers.
The repeated-request median uses the last four samples. Every request returned
HTTP 200, and operation counts were identical across the five samples.

| Page and fixture | First request | Repeat median | DuckDB opens | SQL executions | `table_info` queries | Full package reads |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| File Source data, `/sources` | 1.68 s | 1.48 s | 7 | 207 | 108 | 4 |
| Saved tables, `/datasets` | 3.05 s | 1.09 s | 23 | 165 | 90 | 2 |
| File Odoo data, `/schema` | 0.68 s | 0.60 s | 7 | 343 | 204 | 3 |
| Related tables, `/derived-entities`, no rules | 1.28 s | 1.14 s | 27 | 217 | 128 | 2 |
| Related tables, five saved lookup rules | 7.40 s | 3.83 s | 77 | 557 | 313 | 12 |
| Odoo capture choices, `/sources` | 0.65 s | 0.47 s | 7 | 250 | 157 | 1 |
| Odoo model selection, `/schema` | 0.84 s | 0.72 s | 7 | 344 | 205 | 2 |

These are instrumented HTTP test timings on Windows with Python 3.14.7 and
DuckDB 1.5.5. They exclude browser rendering, asset requests, and TCP transport.
The first request is not a controlled cold-start measurement. Timing varied
between runs; the counts provide stronger evidence of repeated work. These
measurements do not establish production p95 or a promised speed improvement.

SQL counts cover `execute` calls, excluding transaction methods and result
fetching. DuckDB opens count physical database owners, rather than every
repository cursor. The SQL and package timings overlap and must not be added
together.

The [raw samples](../testing/evidence/stage12-page-loading-2026-10-06/baseline.json)
include database timing, SQL counts, response sizes, and `Server-Timing`.
The [audit helper](../testing/evidence/stage12-page-loading-2026-10-06/audit.py)
recreates the fixtures and writes results under `.tmp`. Run it from the repository:

```powershell
.\.venv\Scripts\python.exe docs\testing\evidence\stage12-page-loading-2026-10-06\audit.py
```

## Batch 1: share database opens and move page reads off the event loop

**Priority: highest. Effort: small to medium.**

This batch is implemented. The following paragraphs record the baseline
findings and the approved design.

`/sources` and `/schema` already use `run_page_read`. That helper runs the page
in a worker and retains a database owner while repositories use separate
cursors. Adding a new connection pool would duplicate an existing mechanism.

The `/datasets` GET in
[`sources.py`](../../src/impodo/web/routers/sources.py), the `/derived-entities`
GET in [`derived_entities.py`](../../src/impodo/web/routers/derived_entities.py),
and the adjacent `/target` and `/files` GETs run their synchronous reads directly
inside async handlers. Extend the existing `run_page_read` pattern to their
complete read-and-render callbacks. Preserve redirect checks and form state.

For the measured saved-table and related-table fixtures, aim to reduce the
physical opens from 23, 27, or 77 to at most 7 before changing middleware.
The open count should remain constant as saved rules increase. This is a
proposed regression budget based on the existing scoped-page pattern.

There is also shared overhead before each page callback.
`WorkspaceAccessMiddleware.dispatch` offloads access resolution, but then
calls `workspace_route_policy` synchronously on the event loop. The policy in
[`app.py`](../../src/impodo/web/app.py) reads the canonical workspace and verifies
its store. These database reads can delay other requests.

Run access resolution and its local route policy in one bounded synchronous
worker operation, with the verified access context bound inside it. Reuse
database owners within that operation and release them before awaiting the
page. For ordinary open Authoring fixtures, target at most two opens during
these checks and four during page construction, or six in total. Closed
workspaces and recovery routes need separate budgets.

Keep the existing Project authorization, closed-workspace redirects, Recipe
journey restrictions, and trusted job-route handling. Do not retain a DuckDB
handle across an await or an Odoo call.

## Batch 2: read and verify the page's source and owner data once

**Priority: high. Effort: medium.**

The source-package, workspace-state, setup-selection, and safe credential-status
reuse is implemented. Reuse lasts for one page request. Credential dialogs still
resolve the canonical owner and read a separate status when it differs from the
page's owner. The following findings refer to the baseline implementation.

`MigrationWorkspaceStateRepository.get` reconstructs the full Data version
package while building workspace state. `DataVersionOwnedSourceRepository`
then reconstructs it again for catalogues, configurations, and a selection's
owner check. `FoundationSourcePackageReader.get` reads the Data version registry
record and performs six package-content queries. It reconstructs the objects
and verifies their hashes on each call.

On the file Source data page, this produces four full package reads. The
catalogues and configurations are already reused by `_dataset_choices_from`,
so the remaining duplication lies below that presenter.

Use typed page-query results for source review and schema review. Load
canonical owners and the hash-verified source package once, then pass those
values to workspace-state projection and presentation. Keep SQL inside the
adapters and lifecycle decisions inside their current services.

On the schema GET, `render_schema` reads workspace state and `_render_schema`
reads it again. Pass the already read state into the presenter. Reuse the
page's setup binding for Odoo requirements and Recipe recovery presentation,
instead of independently searching for it several times. Remote pages should
also resolve credential ownership and status once for the page and its dialog.

Aim for one workspace-state construction and one full package reconstruction
per participating Data version in a page request. Preserve Project lineage
and package hash checks on the first read. Use explicit read snapshots within
each store and check the revisions or hashes that connect stores; retained
cursors alone do not provide one transaction across databases. Command
validation must still read current state after mutations.

Relevant owners are
[`migration_workspace_state_repository.py`](../../src/impodo/adapters/duckdb/migration_workspace_state_repository.py),
[`data_version_source_repository.py`](../../src/impodo/adapters/duckdb/data_version_source_repository.py),
[`foundation_source_package_reader.py`](../../src/impodo/adapters/duckdb/foundation_source_package_reader.py),
and [`schema.py`](../../src/impodo/web/presenters/schema.py).

## Batch 3: remove per-rule reads and repeated schema inspection

**Priority: high. Effort: medium.**

Rule batching, capture-plan review, and bulk column inspection are implemented.
The repeated-rule and repeated-plan patterns below describe the baseline code.

For every saved related-table rule, `_render_derived_entities` invokes
`DerivedEntityWorkspaceService.preview` or `preview_related`. Each preview
rereads the source selection and catalogues. In the measured fixture, each
additional rule adds ten database opens, two package reads, and 68 SQL
executions. Five rules therefore add 50 opens and ten package reads.

Load selection and catalogues once, then evaluate all rule previews against
those same verified inputs. Add an application method that produces the
complete preview set, retaining each rule's preview and error. SQL count must
remain constant as the number of rules grows; the in-memory preview calculation
may still grow with the number of rules.

The Odoo capture presenter has another repeated-set pattern.
`_render_odoo_capture_selection` calls `validate_current_plans` inside its loop
for each selection with a protected filter. That service rereads workspace
state, schema, and selections, then validates every selection. With N filtered
models, this can perform N whole-set validations and approximately N squared
plan evaluations. This finding comes from the code; the baseline fixture has
no saved filtered plans.

Validate the set once and return errors keyed by model. Preserve the current
model-specific messages, encrypted-filter verification, capture roles, and
complete-set checks. Do not replace this with one global error that prevents
the operator identifying the invalid plan.

Finally, repository reads repeatedly inspect the same physical structures.
The schema page performs eight workspace-engine schema checks. Structural
validators also query `PRAGMA table_info` once per table in the workspace
linkage and Data version stores. Sharing physical opens does not eliminate
these inspections.

The implementation replaces per-table metadata queries with one catalogue
query that reconstructs the same ordered column checks. The exact table set,
generation, version, and lineage checks remain in their existing owners.
Temporary tables retain the same shadowing behavior as unqualified
`PRAGMA table_info` reads. Attached databases do not supply a store's columns.

Every validation remains fresh. This avoids an invalidation mechanism and
continues to reject a changed structure on the same retained connection.
Malformed-store rejection, unsupported generations, and forward upgrades must
remain covered by tests. Command transaction behavior remains independent.

Relevant owners are
[`derived_entities.py`](../../src/impodo/application/workspace/derived_entities.py),
[`sources.py`](../../src/impodo/web/routers/sources.py),
[`odoo_source_capture_service.py`](../../src/impodo/application/odoo_source_capture_service.py),
[`workspace_engine.py`](../../src/impodo/adapters/duckdb/schema/workspace_engine.py),
and [`unit_of_work.py`](../../src/impodo/adapters/duckdb/unit_of_work.py).

## Batch 4: bound optional content if measurements justify it

**Priority: after the database changes. Effort: small to medium per item.**

The Odoo capture page loads and renders all capture history. Add a bounded
recent-history query and pagination when mature-workspace measurements show
growth. Preserve access to older evidence through the history page.

The schema page renders all model choices and detailed field tables, including
collapsed sections. Collapsing HTML does not avoid its construction. Measure
response bytes and browser render time with a large model catalogue before
moving detailed field tables to a bounded, authorized request on expansion.
Preserve selected form values and complete governance submissions.

`LoopbackSecurityMiddleware._secure` currently assigns `Cache-Control:
no-store` to every response, including static CSS and JavaScript. Measure the
asset waterfall. If repeated downloads contribute materially, allow caching
only for public, versioned static assets. Keep authenticated HTML, source
values, protected evidence, credentials, and job responses uncached.

No per-record Odoo calls were found in the normal page-loading paths examined.
Schema refresh, source inspection, row counting, and record freezing are
explicit operations and should be measured separately. Capture progress and
status routes already use bounded job projections; preserve that path.

## Verification and proposed acceptance

Extend the existing request diagnostics with value-free query counts and
durations, package-read counts, page-query time, and worker queue delay.
The current schema timing counter covers workspace-engine checks, rather than
every store validator. Include linkage and Data version checks when reporting
total structural overhead. Never record SQL parameters, business values, or
credential material.

After each batch, rerun the archived small fixture and the existing focused
tests for page-read connections, source workflow, schema recovery, derived
entities, and Project security. Add meaningful regression coverage for:

- SQL and physical-open counts that remain constant with 0, 5, and 25 rules.
- One complete plan review with 1, 5, and 10 filtered Odoo models, respecting
  the existing ten-model limit.
- Fresh source choices and pending schema changes after another tab saves.
- Unchanged rejection of corrupt hashes, unsupported schemas, and cross-wired stores.
- Released file handles after both successful reads and exceptions.
- Unchanged source-model choices, business keys, redirects, and preview errors.

Then measure uninstrumented HTTP and real browser navigation on representative
small and large fictional workspaces. Include many files, a large Odoo model
catalogue, substantial capture history, and a mature workspace with at least
25,000 prepared records. Record first-process requests separately from repeated
requests and take enough samples to calculate p50 and p95, initially at least
30 per page and state. Record hardware and concurrent work.

Proposed desktop targets are a repeated-request p95 below one second for the
ordinary Source data, saved-table, Odoo data, and capture-choice pages, and
below 1.5 seconds for the related-table page with 25 rules. Target a first-process
request below three seconds. During construction, target authenticated health
and active-job status p95 below 250 milliseconds, with every request below one
second. These targets require platform qualification and are not achieved
guarantees.

Measure improvement against the same fixtures on the same machine. Report
elapsed time and operation counts together. A faster page must still show the
current choices and enforce the same evidence and authorization boundaries.

## Related documentation

- [Source data implementation](../developer/workflow/01-source-data.md).
- [Odoo data implementation](../developer/workflow/02-odoo-data.md).
- [Workflow evidence lifecycle](../developer/contracts/evidence-lifecycle.md).
- [Existing workflow responsiveness qualification](responsive-workflow-liveness-and-navigation.md).
