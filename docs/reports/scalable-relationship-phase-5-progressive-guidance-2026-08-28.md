---
audience: developer
kind: report
status: current
---

# Scalable relationship planning Phase 5 progressive guidance — 2026-08-28

## Decision supported by this report

Phase 5 makes the accepted dependency plan understandable to a data manager
without adding a second planner, a generic workflow engine, or Product and
bill-of-materials branches. The data manager still controls included rows,
business keys, mappings, and optional relationships. Changing those choices
and comparing again produces another safe order.

This result supports starting Phase 6. It does not raise the current
related-data limit, qualify the 16,000-Product and 80,000-BOM-line shape, or
authorize a Production load.

## Compact load-order guidance

`ExecutionService.current_preview` derives a disposable presentation from the
immutable `ExecutionSnapshot.relationship_plan`. It shows at most five
numbered load groups, with a record count and at most three prepared-data labels
per group. It reports how many later groups were omitted and whether reviewed
optional relationships require a second pass.

The projection contains no source values or row identifiers and never becomes
execution authority. The browser explicitly tells the data manager that the
order reflects the current mappings, rows, and optional relationships. This
keeps the design useful for a complex BOM without imposing a fixed BOM
workflow.

## Grouped blocker guidance

Stable snapshot blocker codes are grouped into at most five business-language
categories. Each category shows the affected-record count, at most three
prepared-data labels, why loading cannot proceed, and one next action. Missing
or duplicate supporting rows, incomplete references, model mismatches, blocked
dependencies, and required relationship cycles have distinct guidance.

An unknown blocker code uses one conservative review message. It does not
create an exception or bypass the existing fail-closed `scope_error` gate.

## Journal-derived progress

`LoadJobManager` now names the current load group and carries the number of new
records expected to need relationship completion. Its progress projection
treats `PLANNED`, `IN_FLIGHT`, and `RETRY_READY` rows as unfinished first-pass
work. A `PARTIALLY_APPLIED` row has an accepted create receipt but remains
unfinished until its reviewed relationship fields are saved.

The page therefore cannot enter relationship completion while any first-pass
row remains unfinished. It reports final write results separately from
accepted creates and updates, and shows completed and pending relationship
records without calling them verified before read-back.

## Browser and documentation evidence

The authenticated local browser rendered a synthetic Product/BOM-shaped
preview at 1440×1024. The visible order was units and categories, Products and
variants, BOM headers, then BOM component lines. The confirmation page repeated
the exact target and write count and explained the bounded second relationship
step. The following current screenshots were refreshed only after the browser
implementation and focused tests passed:

- [`17-load-preview.png`](../images/user/17-load-preview.png)
- [`17b-load-confirmation.png`](../images/user/17b-load-confirmation.png)

The paired user and developer workflow pages, execution contract, workflow
registry, Python code map, acceptance strategy, and accepted implementation
plan now describe the same boundary.

## Verification evidence

The following checks passed on 2026-08-28:

- 132 focused snapshot, scheduler, dependency, execution, reconciliation,
  load-job, DuckDB journal, browser, and dependency-baseline tests passed.
- A journalled in-flight row remains in first-pass loading, does not count as a
  final result, and cannot trigger relationship completion.
- Optional-cycle previews expose the exact affected-record and field counts.
- Required cycles and missing incoming rows expose distinct grouped actions.
- The browser renders the compact order and grouped blocker copy without an API
  key or source values.
- Ruff passed for the Phase 5 Python code and focused tests.
- The load polling JavaScript passed syntax validation.
- Documentation quality and exact workflow-symbol checks passed.

The repository-wide run executed 995 tests with 13 optional skips. It was not
a clean acceptance run: the worktree changed while the suite was running, so
source-worker checks correctly reported that Impodo had been updated in the
open process. The run finished with 10 failures and 12 errors, including
concurrent correction-store deletion ownership and previously recorded mapping
template organization guards. None reproduced in the 132-test Phase 5 scope,
which passed again after the broad run completed.

## Remaining boundary

Phase 6 owns representative Product/BOM scale and disposable Odoo 19
qualification. No Odoo API key was needed for Phase 5 because this phase only
projects already frozen snapshot and journal evidence. A disposable Odoo 19
database remains necessary to measure real permissions, request count,
wall-time, memory, restart, and read-back behavior at the representative
scale.
