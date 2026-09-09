---
audience: developer
kind: report
status: current
---

# Recipe workflow phase 2: delivery control expectations

This implementation closes the missing Fresh data control-value path identified
in the [first stabilization report](recipe-workflow-stabilization-2026-09-09.md).
It gives developers the behavior, compatibility boundaries, and verification
needed to review this phase. Full migration acceptance remains separate work.

## Resulting behavior

Fresh data now requests every delivery-specific expected total before accepting
the source data. Each prompt identifies the Recipe, source table, unit, and
tolerance. Invariant totals are read-only. Parameters may share one answer
across compatible Recipes; controls always keep separate Recipe-scoped answers.

One `TestRunValues` record saves typed parameters and controls atomically under
an optimistic revision, actor identity, timestamp, and content hash. Version 2
also pins the selected Recipe revisions and semantic hashes. A shared domain
normalizer validates compiler and Fresh data inputs, rejects non-finite totals,
and bounds decimal expansion. Activation reads answers once and passes separate
parameter and control maps through the existing compiler contract.

Accepted totals remain fixed in the compiled mapping baseline. The matching
form displays them as read-only, preserves their definitions when parsing
other decisions, and cannot overwrite them through submitted form values.
The application submission policy also rejects direct total changes.
Activation replay and required-default recovery retain saved control values.

## Compatibility

Migration registry schema version 6 permits both run-value contract versions.
The forward upgrade retains the existing table name, rows, JSON, and hashes;
it does not rewrite old answers as new evidence. New writes use version 2.

An older editable setup may explicitly add missing controls once after source
acceptance while preserving its previously accepted parameters. The same
domain rule governs service validation and the repository transaction. An
active run cannot add these answers. Older application mappings that lacked
totals before compilation retain their existing recovery behavior.

The class and repository method names now describe run values rather than
parameters alone. There is one persistence path for both types of answer.
The production import graph adds one domain module and retains no runtime
cycles or application-to-adapter imports.

## Verification

The 66 focused checks for application behavior, persistence, upgrades, mapping
forms, and architecture passed. The authenticated request journey passed
separately after its fixture was completed, including local preparation and
evaluation of the accepted total against the staged rows. Another 30 lifecycle
tests passed across Test setup, Recipe compilation, default recovery,
Production rollout, and representative Recipe shapes. Documentation and diff
checks passed. These are targeted runs, not the full repository suite.

The test-organization check retains its pre-existing failure:
`tests/integration/web/test_mapping_workflow.py` has 2,096 lines, above its
2,000-line limit. That file is unchanged in this phase.

Focused evidence lives in:

- [Fresh data control tests](../../tests/application/run/test_fresh_data_controls.py),
  including scoping, validation, stale revisions, frozen-answer preservation,
  compiler inputs, real persistence, and a version 5 upgrade with saved answers.
- [Authenticated request test](../../tests/integration/web/test_fresh_data_controls.py),
  covering upload, inspection, rejected missing totals, source acceptance,
  actual Recipe materialization, the read-only matching summary, and evaluation
  of the accepted total during preparation. It uses
  fictional captured Odoo metadata and performs no external Odoo request.
- [Mapping form tests](../../tests/integration/web/test_mapping_forms.py),
  which verify that unrelated form decisions preserve accepted totals and
  invariant control definitions even when a total is submitted in the form.
- [Mapping evidence tests](../../tests/integration/duckdb/test_recipe_application_evidence.py),
  which verify that direct changes cannot rebind accepted totals.

The [Fresh data screenshot](../images/user/03a-fresh-data-control-totals.png)
was captured from the authenticated current application in headless Edge at
1440 by 1024 and inspected. The reproducible
[capture helper](../../scripts/capture_fresh_data_controls.py) uses isolated
fictional data and the current routes.

No live Odoo write, qualification-to-Production acceptance run, or throughput
and memory benchmark is included. This phase removes a missing workflow input
and the resulting return to field authoring; it does not claim a measured
overall speedup.

## Next boundaries to address

The subsequent [target-review phase](recipe-workflow-phase-3-2026-09-09.md)
records the implemented follow-up to the focused-review boundary below.

The remaining full-matcher fallback still exposes authoring controls for some
scoped and composite target decisions. Those decisions need constrained run
views. Durable recovery from lost final worker notifications, remaining web
orchestration ownership, Production setup alignment, and representative
performance measurements remain open from the original review.

The paired [user guide](../user/guides/integrated-test-runs.md),
[developer workflow](../developer/workflow/07-integrated-test-runs.md), and
[lifecycle contract](../developer/contracts/integrated-run-lifecycle.md)
describe the implemented ownership and recovery rules.
