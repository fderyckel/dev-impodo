# Complete Windows qualification for relationship execution

## Status and authority

**Status:** The generic dataset and row planner, bounded execution, component
recovery, and browser guidance are implemented. Qualification at 25,000
scheduled records passed on macOS; the clean Windows repeat remains open as
of 2026-09-15.

The [Load into Odoo developer workflow](../developer/workflow/06-load-into-odoo.md#current-relationship-ordering)
and [execution contract](../developer/contracts/execution-and-reconciliation.md)
own the current graph, scheduling, receipt, and recovery semantics. Completed
phase records and macOS measurements remain in Git history.

This plan retains qualification at the conservative current derived or
materialized boundary. It does not raise row limits or authorize a Production
load. The 16,000-Product and 80,000-BOM-line workload remains in the separate
[deferred 100,000-row track](remaining-work.md#1-qualify-related-and-mixed-preparation-at-100000-rows).

## Remaining Windows qualification

Run the [current-boundary qualification protocol](../testing/acceptance.md#phase-6-current-boundary-qualification)
for the worker and disposable Odoo 19 paths from a clean revision. Use no more
than 25,000 scheduled records across Products, BOM headers, component lines,
and supporting datasets.

1. Run three fresh execution trials and three fresh production-worker
   first/repeat pairs. Record the revision, runtime, fixture, hashes,
   connector-call classes and counts, wall time, peak memory, artifact sizes,
   snapshot reuse, and worker exit.
2. Verify deterministic dataset and row schedules under input permutations and
   batch-size changes. Acyclic hierarchies and BOMs must require no deferred
   relationship write.
3. Verify existing-target precedence, bounded crosswalk revalidation, optional
   cycle completion, and required-cycle rejection before target I/O.
4. Inject interruption around journal-before-transport and generated-receipt
   publication. Resume only after exact read-back verifies earlier components
   and classifies uncertain rows, without recreating a parent.
5. Compare local returned identifiers and remote External IDs through exact
   final read-back. Retain generated-link and optional generated-record
   projection coverage.
6. Complete the outstanding authenticated eligible-field screenshot capture
   with fictional data and verify the paired user and developer guidance.

## Representative fixture

Include shared units and categories, supported Product templates and variants,
BOM headers and components, a reused component, a multilevel dependency, and
missing, ambiguous, and quarantined supporting rows. Add an optional cycle
outside the BOM business fixture and a required cycle that must block.

Assert exact connector-call classes and documented upper bounds. No target
lookup, crosswalk scan, or read-back may grow one-for-one with source rows.
Retain a measured gate for any remaining per-row write path. Wall time alone
cannot qualify the implementation.

## Completion gate

The clean Windows repeat must pass correctness, deterministic hashes, current
time and memory budgets, bounded request counts, restart recovery, generated
bindings, and exact read-back. Run the relevant architecture, owner, browser,
and full test-discovery checks required by the existing qualification protocol.
Keep older completed evidence readable and reject stale pending previews.

Record the repeat evidence in `docs/testing/` and the current acceptance
references, then remove this plan. Any later limit increase requires separate
qualification and an explicit change to the supported boundary.
