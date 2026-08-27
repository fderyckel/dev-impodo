---
audience: developer
stage: review
status: current
---

# Final review

## Responsibility

Final review captures current target evidence, compares it with eligible
prepared rows, classifies every row, and publishes portable review artifacts
and the exact execution snapshot.

It is read-only with respect to Odoo and does not authorize a write.

## Entry conditions

Prepared, quality, resolution, and normalization evidence must be complete and
bound to the current source, schema, mapping, and compiled plan. The reader
must have the narrow scope derived from the compiled requirements.

## Implementation flow

`summary.py` renders the current local result. `preparation.py` can create the
prepared review input. `preflight.py` invokes `PreflightService.compare`, then
serves the manifest, workbook, and review package.

`PreflightService` freezes the input bindings, plans metadata and record
requests, captures the target fingerprint and snapshot, performs offline
classification, and publishes the report and execution snapshot atomically.

When the operator creates the workbook for a file source,
`PreflightService.review_workbook_evidence` reloads the exact current frozen
input once and requires its hash to match the readiness report. It also loads
the frozen normalization evaluation once and requires that evaluation's
content hash to match the report. `ReviewWorkbookCellEffect` carries only an
eligible row's source trace, field, protected before and after display values,
and confirmed review-group explanation into the workbook adapter.

The reporting writer joins prepared records, normalization effects, and
manifest decisions through the source trace ID. The manifest remains
authoritative for every warning or blocker. The pure
`review_workbook_cell_feedback` function gives a manifest field issue
precedence, then distinguishes an added value from a changed value by using
the frozen normalization effect. A blank without a manifest issue is
informational and never becomes a new blocker. The adapter keeps the final
prepared value in the visible cell and places original-value and rule detail
in an Excel note. The package path makes no Odoo call and performs no per-row
or per-cell repository lookup.

For an Odoo source, `review_workbook_evidence` returns no portable value
projection. The workbook continues to use the redacted manifest, while exact
business values remain in the protected comparison artifact.

`PreflightRequirementPlan` also retains each supporting read as a
`ReferenceReadRequirement`. The requirement names the captured parent model
and relationship field, related model, ordered key and scope, requested
fields, and reference-policy hash. `target_readers.py` re-authorizes every
requirement against current captured metadata before Odoo is contacted. It
does not infer authority from the flattened union of requested fields.

For `ODOO` source mode, `build_odoo_comparison_publication` follows a separate
pinned-ID branch. It verifies the protected capture origins once and loads the
frozen Parquet baseline once. It then groups exact IDs into fixed 500-record
requests. Before reading values, Impodo re-probes the governed JSON-2 read
credential and verifies the target, principal, permissions, and context.
Impodo stores baseline, proposed, current, numeric ID, and write-date evidence
only in an AES-GCM-protected artifact. It redacts the portable manifest and
persisted record snapshot. This branch exposes no business-key lookup, create
fallback, per-row Odoo call, or write connector.

For local Odoo, `_read_readiness_snapshots` requires a matching session
profile. `LocalOdooRecoveryRequired` returns the user to the shared local-Odoo
dialog; `target.py` validates the selected address, database, readiness, and
read-only fingerprint before comparison can resume.

`odoo_read_failures.py` classifies connector, evidence, credential, local
profile, storage, and unexpected failures below the presenters. The summary
presenter maps the stable failure to one owning action. It renders the
read-key form only for missing, rejected, or insufficient read access; schema,
mapping, preparation, transport, and storage failures never open that form.

## Code references

| Role | Code |
| --- | --- |
| Comparison orchestration | [`PreflightService`](../../../src/impodo/application/preflight_service.py) |
| Protected Odoo comparison | [`odoo_comparison_service.py`](../../../src/impodo/application/odoo_comparison_service.py) |
| Protected comparison contract | [`odoo_comparison.py`](../../../src/impodo/domain/odoo_comparison.py) |
| Frozen input | [`frozen_input.py`](../../../src/impodo/domain/preflight/frozen_input.py) |
| Review reports | [`reports.py`](../../../src/impodo/domain/preflight/reports.py) |
| Workbook projection | [`reporting.py`](../../../src/impodo/adapters/artifacts/reporting.py) |
| Browser routes | [`preflight.py`](../../../src/impodo/web/routers/preflight.py) |
| Failure classification | [`odoo_read_failures.py`](../../../src/impodo/application/odoo_read_failures.py) |
| Recovery presentation | [`comparison_recovery.py`](../../../src/impodo/web/presenters/comparison_recovery.py) |
| Local recovery routes | [`target.py`](../../../src/impodo/web/routers/target.py) |
| Local target reader | [`target_readers.py`](../../../src/impodo/web/composition/target_readers.py) |
| Shared recovery dialog | [`_local_odoo_dialog.html`](../../../src/impodo/web/templates/_local_odoo_dialog.html) |

## Evidence and state

For file sources, the target snapshot is target-specific and may contain
protected Odoo IDs. The portable report contains natural identities and the
existing deterministic classifications. The review workbook may also contain
the exact prepared file-source values bound to the report's frozen input. It
contains no numeric Odoo IDs and does not become an independent decision
source. The execution snapshot binds only eligible writes to the exact
reviewed evidence.

For Odoo sources, the persisted target snapshot is redacted. Exact IDs and the
baseline, proposed, and current values live only in the protected comparison
artifact. Portable rows expose `UPDATE`, `UNCHANGED`, or `BLOCKED`. Protected
rows distinguish inaccessible or missing records, a missing baseline, schema
drift, and concurrent changes to intended fields.

## Completion and navigation

Final review is complete only when the current report status is `READY`.
Ambiguous or blocked rows keep the stage in **Needs attention**. File-source
load requires a ready report. Odoo-source load remains unavailable under the
current same-database pinned-update policy even when every checked row is safe.

The ready report remains owned by this DataVersion and cannot qualify or amend
a Recipe revision. Applying published rules in another run will require a
fresh comparison against that run's target; that application workflow belongs
to the integrated Test workflow.

## Invalidation and recovery

Source, schema, mapping, compiled plan, prepared data, target fingerprint, or
dependency-order changes make the result stale. A transport HTTP status is not
the domain cause; retain the nested connector error and avoid automatic retries
when target state is uncertain.

Generated workbooks and packages are immutable outputs. Regenerate them from a
new comparison rather than editing their manifest.

## Odoo 19 and performance

The preflight planner groups metadata and record reads by target model. Keep
domains bounded and reject unrestricted record requests. Adding one
`search_read` per prepared row is an N+1 correctness and performance defect.

Remote comparison performs at most one exact captured-schema identity probe
and one combined supplemental-model identity probe. Local comparison receives
only the authorized supplemental models named by the plan, not every relation
present in captured metadata. Source-row count must not increase either probe
count.

Target reads must use the narrow Odoo 19 read connector. No generic method call
and no write method belongs in this stage.

Workbook creation may load the complete eligible prepared set once because the
XLSX output contains one review row per decision. It may also load the complete
frozen normalization effect ledger once to explain those cells. Keep both
loads bounded to the exact current run. Index source traces and field effects
once in memory. Do not reopen repositories for individual workbook rows or
cells, and do not contact Odoo while writing them.

## Verification

- [`tests/application/workspace/review/test_preflight.py`](../../../tests/application/workspace/review/test_preflight.py)
- [`tests/domain/preflight/test_review_workbook.py`](../../../tests/domain/preflight/test_review_workbook.py)
- [`tests/performance/test_preflight_scale.py`](../../../tests/performance/test_preflight_scale.py)
- [`tests/integration/artifacts/test_reporting_cli.py`](../../../tests/integration/artifacts/test_reporting_cli.py)
- [`tests/integration/artifacts/test_preflight_outputs.py`](../../../tests/integration/artifacts/test_preflight_outputs.py)
- [`tests/integration/odoo/test_connectors.py`](../../../tests/integration/odoo/test_connectors.py)
- [`tests/application/workspace/review/test_odoo_comparison.py`](../../../tests/application/workspace/review/test_odoo_comparison.py)
- [`tests/integration/web/test_review_workflow.py`](../../../tests/integration/web/test_review_workflow.py)

Verify fixed classification precedence, batched requests, portable identities,
snapshot completeness, stale bindings, deterministic artifacts, and absence of
write capabilities.

## Related documentation

- [User guide: Final review](../../user/workflow/05-final-review.md)
- [Preflight contract](../contracts/preflight.md)
- [Quality and quarantine contract](../contracts/quality-and-quarantine.md)
- [Architecture decisions](../../decisions/README.md)
- [Recipe and data-version lifecycle contract](../contracts/recipe-lifecycle.md)
