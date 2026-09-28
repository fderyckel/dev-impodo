# Retire relationship execution qualification

## Status and authority

**Status:** The generic dataset and row planner, bounded execution, component
recovery, and browser guidance are implemented. Qualification at 25,000
scheduled records passed on macOS and Windows. The authenticated eligible-field
screenshot is current. Plan retirement remains open because the clean
repository-wide test discovery is not green as of 2026-09-28.

The [Load into Odoo developer workflow](../developer/workflow/06-load-into-odoo.md#current-relationship-ordering)
and [execution contract](../developer/contracts/execution-and-reconciliation.md)
own the current graph, scheduling, receipt, and recovery semantics. Completed
phase records and macOS measurements remain in Git history.

This plan retains qualification at the conservative current derived or
materialized boundary. It does not raise row limits or authorize a Production
load. The 16,000-Product and 80,000-BOM-line workload remains in the separate
[deferred 100,000-row track](remaining-work.md#1-qualify-related-and-mixed-preparation-at-100000-rows).

The [Windows qualification report](../testing/relationship-execution-windows-2026-09-28.md)
records three fresh execution runs, three fresh worker first/repeat pairs,
batch-size and input-order determinism, bounded connector calls, interruption
recovery, generated identifiers, exact relationship read-back, artifact sizes,
and the authenticated screenshot. These checks passed from a clean isolated
revision without stopping the operator's running Impodo process.

## Remaining retirement gate

Restore a green clean-revision discovery run. The 2026-09-28 diagnostic run
found independent target-match, preparation, scenario-fixture, browser-copy,
and Polars timezone failures before it stalled in a Production-readiness
browser test. The relationship-owned focused gate passed, but the repository's
retirement rule does not permit that focused result to replace full discovery.

After the baseline is green:

1. Run full test discovery from the clean revision with Windows temporary
   storage and timezone data available.
2. Run the architecture, owner, browser, and documentation checks required by
   the [current-boundary qualification protocol](../testing/acceptance.md#phase-6-current-boundary-qualification).
3. Confirm that the Windows evidence report and paired user and developer
   guidance still describe the current implementation.
4. Remove this plan and its roadmap links.

Any later increase beyond 25,000 scheduled records remains a separate
qualification and requires an explicit change to the supported boundary.
