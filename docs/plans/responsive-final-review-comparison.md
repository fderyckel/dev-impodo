# Keep Final review responsive during large Odoo comparisons

## Status and proposed decision

**Status:** Partially implemented on 2026-09-13. Slices 1 and 2 are implemented,
the one-pass full-preview counter from Slice 4 is implemented, and successful
progress responses now reset the browser recovery counter from Slice 6. Durable
attempt receipts, the persisted compact preview, phase diagnostics, the scale
qualification, and any measured DuckDB publication change remain proposed.

Move **Compare with Odoo** into an observable background job, keep synchronous
repository and artifact work away from the asynchronous web event loop, and
store a compact, hash-bound projection for the **Check changes** page. Preserve
the current atomic comparison publication and require the full immutable
execution snapshot again before any Odoo write can start.

Optimize DuckDB publication only after production-safe phase timings and a
repeatable scale benchmark identify the expensive operation. Increasing the
browser heartbeat timeout alone is not an acceptable fix because it would hide
the delay without making progress visible or keeping page requests responsive.

The intended reader is the developer deciding how to repair Final review and
load-preview responsiveness without weakening comparison or execution safety.

## The problem this solves

When a data manager selects **Compare with Odoo** for a large Product dataset,
the browser currently holds the form request open until Impodo has read Odoo,
classified every row, created two JSON artifacts, and published every decision
to DuckDB. The button changes to **Comparing with Odoo...**, but the page does
not show durable phases or row progress.

An observed Product workspace contained 18,061 eligible rows. Its comparison
finished successfully, but the work after prepared-data approval took about
three minutes. The resulting execution snapshot was approximately 36 MB, the
portable manifest was approximately 8 MB, and DuckDB stored 18,061 individual
decision rows. During that interval, the browser showed **Impodo is not
responding**, although the comparison later published a `READY` result. These
figures are point-in-time diagnostic evidence, not a general performance
guarantee.

The implementation before Slices 1 and 2 had four relevant characteristics:

- `preflight.py::compare_workspace_data` already calls
  `PreflightService.compare` through `run_in_threadpool`, but it leaves the
  browser waiting on the original POST request.
- The same route calls `ExecutionService.current_preview` synchronously after
  comparison. `summary.py::workspace_summary`,
  `execution.py::review_load`, and `execution.py::confirm_load` also perform
  synchronous rendering or preview work directly from asynchronous routes.
- `ExecutionService.current_preview` loads and verifies the complete execution
  snapshot, then scans its rows several times to build a small browser view.
- `PreflightRepository.save_readiness_report` already inserts decisions in
  bounded batches and commits the current pointer atomically. A faster storage
  implementation must retain that transaction boundary.

The health route itself performs only session validation and returns a fixed
response. The browser currently gives each health request two seconds and
shows the disconnected banner after three consecutive failures. That policy
turns temporary event-loop starvation into a false server-failure message.

## The data-manager experience

After the data manager selects **Compare with Odoo**, Impodo should immediately
open a progress page. The page should explain that this step only reads Odoo
and stores review evidence. It should show one honest phase at a time:

1. **Checking the saved setup** verifies the current workspace, approved
   preparation, target, and read access.
2. **Reading Odoo** fetches the bounded metadata and record pages required by
   the compiled plan.
3. **Comparing records** classifies the eligible rows.
4. **Building review evidence** creates the manifest and exact execution
   snapshot.
5. **Saving the comparison** publishes the report, decisions, and target
   snapshots atomically.
6. **Comparison complete** links to **Check changes**.

The progress page must not invent precise row percentages for phases that do
not expose reliable row progress. It can show exact completed and total rows
during classification and publication when those counters are available. For
other phases, it should show the phase and elapsed time.

If Impodo restarts before publication, the previous current comparison remains
unchanged. The data manager should see **Comparison interrupted — safe to try
again**. If publication completed before the restart, Impodo should recover the
new result and continue to **Check changes** instead of asking for another
comparison.

For the observed example, **Check changes** would load a compact summary of
15,578 creates, 2,471 updates, 12 unchanged rows, and zero blockers. Opening
that page would not parse the 36 MB execution snapshot. The final load action
would still load and verify the full snapshot before it creates an execution
journal or contacts Odoo with write access.

## Required invariants

The change must preserve these boundaries:

- Final review remains read-only with respect to Odoo.
- One eligible prepared row still produces exactly one persisted comparison
  decision.
- The manifest remains authoritative for comparison classifications and
  issues.
- The manifest and execution snapshot remain immutable and hash-bound to the
  current source, mapping, preparation, target evidence, and dependency order.
- The new comparison becomes current only in the existing atomic DuckDB
  publication. A partial job cannot replace the previous current report.
- The browser projection is display-only. It cannot authorize execution or
  replace the immutable execution snapshot.
- The load POST reloads and validates the full snapshot, submitted snapshot
  hash, current target, current read credential binding, and write identity
  before a journal or Odoo write is created.
- A repeated comparison submission with the same operation identity returns
  the same active or terminal attempt. It does not launch overlapping work.
- Job records, status responses, diagnostics, and logs contain no API keys,
  Odoo business values, numeric Odoo IDs, or source-row values.
- Odoo reads remain model-grouped and bounded. The change must not introduce a
  connector call inside a source-row loop.

## Proposed design

### 1. Remove blocking work from asynchronous routes

Treat every synchronous service, repository, artifact, and template operation
as blocking unless it is proven otherwise. Use `run_in_threadpool` around the
complete synchronous presenter or service call rather than moving only one
nested operation.

The first patch should cover at least these paths:

- `summary.py::workspace_summary` should call `_render_summary` in the thread
  pool.
- Every `_render_summary` call in `preflight.py` should run in the thread pool.
- The post-comparison `ExecutionService.current_preview` call in
  `preflight.py` should run in the thread pool while the synchronous POST path
  still exists.
- `execution.py::review_load` should run `render` in the thread pool.
- `execution.py::confirm_load` should run both `current_preview` and `render`
  in the thread pool.
- A focused audit should identify the same pattern in other Final review and
  Load into Odoo routes. The patch should not broaden into unrelated workflow
  changes.

This slice is a containment fix. It should ship before the background-job and
storage changes because it is small, reversible, and directly protects the
event loop.

### 2. Add an observable comparison job

Add a `PreflightJobManager` that follows the existing bounded job patterns
used for preparation and Odoo capture. It should allow at most one active
comparison per workspace and should serialize large comparisons through a
small configured worker limit to protect local memory.

The browser contract should become:

- `POST /workspaces/{workspace_id}/summary/compare` validates the form,
  reserves an operation identity, enqueues or retrieves the matching job, and
  returns a `303` redirect to the progress page.
- `GET /workspaces/{workspace_id}/preflight/{job_id}` renders the progress
  page.
- `GET /workspaces/{workspace_id}/preflight/{job_id}/status` returns only the
  bounded control-plane projection.
- A successful status returns the current preflight run ID and redirects to
  `/workspaces/{workspace_id}/load/review`.
- A failed status returns one stable failure code, safe message, and owning
  recovery action. It must continue to use the existing Odoo read-failure
  classification.

Use a dedicated worker thread for the first implementation because the current
Odoo reader contains session-owned credential and connector capabilities that
must not be serialized into a child-process command or job record. The worker
must use a captured, authorized `WorkspaceAccessContext`, and the manager must
hold credential-bearing objects only in process memory. The status model must
never contain them.

After the event-loop fixes, exercise the 25,000-row benchmark while sampling
authenticated health responses. Move the classification and artifact-building
portion into a spawned child process only if the worker thread still starves
the web process. That second design must pass immutable artifact paths and
hashes across the process boundary, never an API key. The parent can perform
the bounded Odoo read and stage target evidence; the child can perform the
credential-free classification, serialization, and publication work.

### 3. Make the comparison attempt recoverable

Persist one small, value-free attempt receipt in the workspace database. The
receipt should bind:

- the operation and job identities;
- the workspace and actor identities;
- the expected workspace revision, frozen-input hash, mapping hash, target
  hash, and read-credential binding hash;
- the current phase, timestamps, safe terminal status, failure code, and
  resulting preflight run ID when known.

The receipt is control evidence, not comparison evidence. The ready report and
its current pointer remain the source of truth.

On restart, recovery should compare the receipt with durable evidence:

- If a matching current report and immutable artifacts exist, mark the attempt
  successful and continue.
- If no matching report exists, mark the attempt interrupted. The previous
  current report remains unchanged, and a fresh comparison is safe because the
  operation cannot write to Odoo.
- If artifacts exist without a matching committed report, remove them through
  bounded orphan cleanup or leave them unreachable for the existing cleanup
  path. Never infer success from artifact presence alone.
- If the receipt bindings differ from the current workspace, mark the attempt
  stale and send the data manager to the owning earlier stage.

### 4. Store a compact load-review projection

Build an `ExecutionPreviewSummary` once from the exact execution snapshot
during comparison. Persist it in the same transaction as the readiness report
under a new `preflight_execution_projection` record keyed by the preflight run
ID. Bind the projection to the execution snapshot semantic hash and root hash.

The compact projection should contain only what the browser needs:

- total create, update, unchanged, blocked, and ambiguous counts;
- per-dataset target model and counts;
- total write and deferred-relationship counts;
- at most five load-order groups and their bounded labels;
- at most five blocker groups and their safe next actions;
- the target display identity and snapshot hashes; and
- no row identity, source value, Odoo ID, credential, or authorization object.

Add `ExecutionService.current_preview_summary` for the Summary and **Check
changes** pages. It should verify the current report and compact projection
bindings without materializing the full execution snapshot. Keep
`ExecutionService.current_preview` as the full, fail-closed path used by load
submission, execution, reconciliation, and support operations.

The full preview projection should also replace repeated row scans with one
pass that builds per-dataset counters and the bounded dependency and blocker
indexes. This protects non-browser callers and historical evidence tools.

### 5. Measure and optimize atomic publication

Add value-free production diagnostics around these phases before changing the
storage implementation:

- frozen-input loading;
- requirement planning;
- Odoo read and snapshot binding;
- comparison classification;
- execution-snapshot construction;
- manifest and execution-snapshot serialization;
- each artifact write;
- decision-row encoding;
- decision, dataset, and target-snapshot insertion;
- current-pointer commit; and
- workspace projection synchronization.

Extend the existing fresh-process preflight scale harness so it retains raw
runs and reports wall time, CPU time, peak memory, artifact sizes, database
size, and each phase. Establish clean Windows and macOS baselines before
setting release budgets.

If decision insertion is confirmed as the dominant publication cost, replace
row-oriented `executemany` with a measured DuckDB bulk path. A preferred
candidate is a bounded Polars batch registered with DuckDB and inserted through
`INSERT ... SELECT` inside the existing transaction. The implementation must:

- preserve canonical row ordering and the existing decision JSON;
- use bounded batches so peak memory does not scale without limit;
- verify the inserted row count before updating `preflight_current`;
- roll back the complete publication on any failure; and
- demonstrate a repeatable gain against the same fixture and runtime before it
  replaces the current path.

Do not split the publication into independently committed row batches. That
would make a partial comparison visible and violate the preflight contract.

### 6. Make liveness messaging operation-aware

Keep `/health` independent of workspace databases and external services. A
successful authenticated progress response should also reset the browser's
consecutive health-failure count because it proves that Impodo is responding.

While a known comparison job is active, the progress page should say
**Comparison is still running** when a poll is delayed. It should show
**Impodo is not responding** only when neither the progress endpoint nor the
health endpoint responds across the recovery window.

Retain a bounded health timeout. Any timeout increase should be supported by
the concurrent health benchmark and should remain a secondary tolerance, not
the primary fix.

## Delivery slices

### Slice 0 — Measurements and regression reproduction

Add phase diagnostics, a large deterministic comparison fixture, and a
concurrent health probe. Record the current baseline without accepting it as a
performance target.

### Slice 1 — Event-loop containment

Move synchronous summary, preview, and render calls into the thread pool. Add
route tests that hold a synchronous dependency and prove that `/health`
continues to respond.

### Slice 2 — Background comparison and progress

Add the job model, manager, routes, progress template, polling behavior, one-
active-job rule, and safe failure presentation. Keep the current comparison
service and atomic publication contract.

### Slice 3 — Durable attempt recovery

Add the workspace schema upgrade, repository, restart recovery, stale-binding
checks, and orphan handling for value-free comparison receipts.

### Slice 4 — Compact review projection

Persist the hash-bound projection and switch Summary and **Check changes** to
the lightweight read path. Keep full snapshot validation at load submission.

### Slice 5 — Measured publication optimization

Prototype the DuckDB bulk path, compare clean-process baselines, and adopt it
only if it improves the dominant phase without increasing peak memory or
changing hashes and decisions.

## Acceptance checks

The implementation is acceptable when all of the following are true:

- The comparison POST redirects to a progress page without waiting for Odoo
  reads, classification, artifact creation, or DuckDB publication.
- A successful 25,000-row comparison produces the same manifest hash,
  execution-snapshot hash, classifications, row order, and target snapshot
  bindings as the synchronous reference path.
- Authenticated health and comparison-status requests remain responsive during
  the scale run, and the browser does not show a false disconnected banner
  while status responses are arriving.
- Refreshing or reopening the progress page shows the same active or terminal
  attempt.
- Submitting the same operation identity twice never starts two comparisons.
- A process interruption before commit leaves the previous current report and
  load preview unchanged.
- A process interruption after commit recovers the matching successful result.
- The Summary and **Check changes** routes do not materialize the full execution
  snapshot.
- Load submission still materializes and validates the full immutable snapshot
  and rejects a stale submitted hash before creating a journal or writer.
- Job payloads, receipts, logs, and diagnostics contain no credential or
  business value.
- Odoo connector call counts remain bounded by the requirement plan and do not
  grow once per source row.
- The measured storage replacement, if adopted, retains one atomic commit and
  shows a repeatable improvement in raw clean-process runs on both supported
  development platforms.

## Expected code ownership

| Responsibility | Proposed or affected code |
| --- | --- |
| Comparison job model, phases, and manager | `src/impodo/application/preflight_jobs.py` |
| Worker composition | `src/impodo/web/app.py` |
| Compare and progress routes | `src/impodo/web/routers/preflight.py` |
| Progress page | `src/impodo/web/templates/workspace_preflight_progress.html` |
| Browser polling | `src/impodo/web/static/job-polling.js` |
| Event-loop containment | `src/impodo/web/routers/summary.py`, `src/impodo/web/routers/preflight.py`, and `src/impodo/web/routers/execution.py` |
| Compact projection | `src/impodo/application/workspace/execution/service.py` and `src/impodo/adapters/duckdb/preflight_repository.py` |
| Attempt and projection schema | `src/impodo/adapters/duckdb/schema/preflight.py` and the workspace-engine forward upgrade |
| Liveness behavior | `src/impodo/web/static/server-recovery.js` |
| Production-safe timings | `src/impodo/application/preflight_service.py` and `src/impodo/web/diagnostics.py` |

## Verification scope

Add or extend focused tests in these areas:

- `tests/integration/web/test_review_workflow.py` for enqueue, progress,
  duplicate submission, failure presentation, and redirects;
- `tests/integration/web/test_load_workflow.py` for the compact browser preview
  and full-snapshot load gate;
- `tests/integration/web/test_diagnostics.py` for operation-aware health
  recovery;
- `tests/application/workspace/review/test_preflight.py` for phase callbacks,
  cancellation boundaries, and unchanged evidence;
- a new application test module for job state and restart recovery;
- `tests/integration/duckdb` coverage for the attempt and compact-projection
  schema, atomic publication, rollback, and forward upgrade;
- `tests/performance/test_preflight_scale.py` and
  `tests/performance/preflight_scale_runner.py` for retained raw timings,
  concurrent liveness, peak memory, and candidate storage comparisons; and
- `tests/e2e/test_project_setup_journey.py` for the data-manager path from
  **Compare with Odoo** through progress to **Check changes**.

After implementation, update the paired Final review and Load into Odoo user
and developer pages, `docs/workflow.yml`, the preflight contract if the durable
attempt becomes normative, and the authenticated screenshots for the new
progress decision point.

## Out of scope

This proposal does not change classification semantics, target identity rules,
Odoo 19 connector scope, load batching, execution idempotency, reconciliation,
or the correction and Odoo-to-Odoo transfer workflows. It does not introduce
parallel Odoo comparisons for one workspace. It also does not treat heartbeat
tuning as evidence that the comparison itself became faster.

## Related documentation

- [User guide: Final review](../user/workflow/05-final-review.md)
- [Developer implementation: Final review](../developer/workflow/05-final-review.md)
- [Preflight contract](../developer/contracts/preflight.md)
- [User guide: Load into Odoo](../user/workflow/06-load-into-odoo.md)
- [Developer implementation: Load into Odoo](../developer/workflow/06-load-into-odoo.md)
- [Execution and reconciliation contract](../developer/contracts/execution-and-reconciliation.md)
- [Documentation style guide](../style-guide.md)
