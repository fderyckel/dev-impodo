# Finish comparison recovery and qualify Final review performance

## Status and remaining scope

**Status:** Background comparison, event-loop containment, one-pass full-preview
counts, compact navigation evidence, and operation-aware liveness are
implemented. Durable comparison attempts, the remaining detailed compact load
preview, and measured publication qualification remain open as of 2026-09-15.

Current behavior belongs to the [Final review developer workflow](../developer/workflow/05-final-review.md),
[Load into Odoo](../developer/workflow/06-load-into-odoo.md), and
[preflight contract](../developer/contracts/preflight.md). Completed delivery
records remain in Git history.

`ExecutionPreviewSummary` is already published atomically with comparison
readiness and is used by shared navigation. Reuse that evidence when completing
the detailed preview; do not create another navigation-owned source of truth.

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

## Remaining design

### 1. Make the comparison attempt recoverable

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


### 2. Complete the compact load-review projection

Extend the existing hash-bound summary only where the **Check changes** page
still needs full-snapshot detail. Preserve current source, mapping, preparation,
target, credential, snapshot semantic-hash, and snapshot-root bindings.

The bounded review should include per-dataset models and counts, total writes
and deferred relationships, at most five load-order groups, and at most five
blocker groups with safe next actions. It must contain no row identity, source
value, Odoo numeric identifier, credential, or authorization object.

Use the compact path for ordinary rendering and keep full immutable-snapshot
validation for load submission and execution. Historical evidence without a
usable summary must fail closed or use an explicit bounded recovery path. The
one-pass full-preview counters already exist and are regression coverage.

### 3. Measure and optimize atomic publication

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


## Remaining delivery order

1. Record phase diagnostics, clean-process baselines, and concurrent health
   latency on Windows and macOS.
2. Add the durable attempt receipt, schema upgrade, restart recovery,
   stale-binding rejection, and orphan handling.
3. Complete the detailed compact preview through the existing summary owner.
4. Adopt a bounded DuckDB publication change only if the same fixture proves a
   repeatable gain without changing decisions, hashes, or atomic visibility.

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


## Ownership and verification

Comparison attempts belong to `application/preflight_jobs.py`, the preflight
repository, and workspace schema upgrades. Preview meaning belongs to
`application/workspace/execution/service.py`; publication belongs to
`adapters/duckdb/preflight_repository.py`. Phase timings belong to the owning
application operation and the privacy-safe diagnostic recorder.

Extend the existing review, load, preflight-job, repository, diagnostics, and
`tests/performance/test_preflight_scale.py` checks. Verify interruption before
and after commit, duplicate operation identities, stale summaries, full-snapshot
load validation, atomic rollback, bounded connector calls, and responsive health.
Retain raw phase timings, memory, and artifact sizes from repeated fresh runs.

Update paired user and developer pages, the preflight contract, workflow
registry, and authenticated screenshots after the remaining behavior ships.
The change does not extend Odoo connector scope or alter classification,
execution idempotency, reconciliation, or correction semantics.
