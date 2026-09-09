---
audience: developer
kind: report
status: current
---

# Recipe workflow phase 4: recover published preparation

This report gives developers the behavior and evidence needed to review
preparation recovery after [phase 3](recipe-workflow-phase-3-2026-09-09.md).
When a worker saves its result but loses its final notification, Impodo can
now reopen the saved review without preparing the delivery again. Returning
to the run also repairs a missed registry notification after session state
has been lost.

## Behavior and ownership

The application-owned `PreparationRecoveryService` collects current input
metadata. Its DuckDB repository checks publication headers, current pointers,
and dependency bindings in one query. A current duplicate evaluation with
candidates restores duplicate review. A complete quality and normalization
chain restores prepared-data review. Bounded preparation must have published
its session; a normalization header alone cannot prove that publication
finished. The existing materialized path remains supported.

The worker supervisor checks durable evidence only after the child exits and
its queue has been drained without a terminal event. Explicit failure and
cancellation events retain their meaning. Missing, invalidated, mismatched,
or unreadable evidence leaves an unexpected exit retryable. The worker slot
is released even when the evidence read fails.

Run entry, application continuation, and preparation requests may inspect the
first unverified application's saved result. Active jobs, blocking issues,
load progress, and later milestones prevent recovery. Status polling remains
a bounded registry and memory read. Restoring a session snapshot starts no
worker. The manager checks the observed attempt identity under its lock so
that a newer attempt cannot be overwritten. Milestone publication compares
the application mapping hash inside the registry transaction.

The coordinator rechecks duplicate evidence even when a terminal session
snapshot exists. Once duplicate review is approved, preparation can continue
instead of returning to the old review. Retry also retains the mapping hash
in the worker request, closing the previous retry route's missing binding.

`web/run_commands.py` now owns enqueue, retry, recovery, and run milestone
coordination. `web/run_review.py` only builds the presentation. Preparation,
resolution, integrated-run, and execution routes use that common command
owner. This removes the dependency cycle found during implementation. The
coordinator still uses `WebContext`; moving it behind explicit application
ports remains a possible later refinement.

The reviewed inventory contains 423 production modules and 2,463 runtime import
edges, with no cycles or application-to-adapter imports.

## Efficiency and boundaries

Recovery does not scan source rows, load all saved row evidence, contact Odoo,
or repeat preparation. It reads only the current eligible workspace, never
every workspace on each status poll. Async routes run the commands in the
thread pool. No schema upgrade, new job database, or durable event journal is
introduced.

Publication binding checks do not replace downstream artifact validation.
After the session registry is lost, Impodo recovers current published work;
it does not reconstruct the full history of an interrupted attempt. Recovery
grants no comparison, load, verification, qualification, or Production
authority. These tests establish avoided work and bounded reads, not a
measured whole-workflow throughput or peak-memory improvement.

## Verification

Across the focused runs, 84 distinct checks passed. One existing browser
assertion remains failing, as described below. Documentation and diff checks
also passed.

- [Worker and coordinator tests](../../tests/application/workspace/preparation/test_recovery.py)
  exercise lost events, incomplete evidence, read failures, mapping mismatch,
  explicit failure and cancellation, idempotent restoration, concurrent
  attempts, dependency order, duplicate approval, and load-progress guards.
- [Publication lifecycle test](../../tests/integration/duckdb/test_preparation_recovery.py)
  publishes actual duplicate candidates, recovers their review, rejects stale
  bindings, and stops returning that destination after decisions are frozen.
- [Authenticated recovery journey](../../tests/integration/web/test_recipe_preparation_recovery.py)
  uses the real compiler and a spawned preparation worker. It deliberately
  drops the final worker event and fails terminal registry notification, then
  replaces the session job registry. Continuing the run opens the same saved
  normalization result without spawning another worker. Changed inputs,
  incomplete session publication, invalidated results, and stale mapping
  milestone updates are rejected. Repeated status polls perform no recovery.
- Existing preparation job, stabilization, integrated-run ordering, dependency,
  and identity checks cover the changed command ownership.

The [recovered review screenshot](../images/user/03c-recovered-recipe-review.png)
was captured from the authenticated current UI in headless Edge at 1440 by
1024 and visually inspected. The [capture helper](../../scripts/capture_recipe_preparation_recovery.py)
uses isolated fictional data and a fresh session job registry. It verifies
that returning to the run starts no worker. The fixtures make no external
Odoo call or write.

The broader preparation browser module has one outstanding failure:
`test_prepare_rejects_bad_source_hash_before_publication_and_redirects` expects
“Stored source selection is invalid”, while its worker reports “The prepared
columnar snapshot could not be verified”. Both runs reject the evidence and
publish no preparation result. The same assertion failure was reproduced
with the committed enqueue implementation from `4791d08`, using an isolated
fixture without reverting the working tree. The other eight tests in that
browser module passed. This phase does not repair that older fixture.

The full repository suite and live Odoo acceptance were not run. The previously
reported test-organization failure in the unchanged 2,096-line mapping workflow
test remains outside this phase.

## Remaining work

Production readiness and activation recovery are addressed in
[phase 5](recipe-workflow-phase-5-2026-09-09.md). Full Production source-flow
alignment and representative qualification-to-Production acceptance remain
workflow priorities. Whole-workflow timing and
peak-memory measurements still need representative delivery sizes. Recovery
of load journals remains governed by the existing execution services.
