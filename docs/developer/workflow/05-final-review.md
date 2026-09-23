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
prepared review input. In the production launcher, `preflight.py` enqueues
`PreflightService.compare` through `PreflightJobManager`, redirects immediately
to a progress page, then serves the manifest, workbook, and review package.
The manager serializes comparisons, permits one active attempt per workspace,
and binds the already authorized `WorkspaceAccessContext` in its worker thread.
Credential-bearing reader closures remain only in process memory; job state and
status responses contain no credential or business value.

`PreflightService.compare` emits coarse `VERIFYING`, `READING`, `COMPARING`,
`BUILDING`, and `PUBLISHING` phases. They are operational progress, not durable
comparison evidence. A success status names the published preflight run and
continues to the load review. Failures retain the existing typed Odoo read
classification and safe recovery presentation. Rapid repeat submissions reuse
the active attempt. After a process restart, unfinished in-memory job state is
gone; the existing atomic report publication still leaves either the previous
current report or the complete new report, never a partial current report.

Tests may disable the manager and exercise the synchronous compatibility path.
Both paths call the same comparison service and publication transaction.

The summary presenter groups the quality findings on the current review page
by reason, message, and friendly field label. It labels each group as a direct
finding or an inherited dependency and states that the counts are page-scoped.
The row table renders every issue instead of collapsing the record to its first
issue. Stable reason and rule identifiers remain under **Support details**.

`PreflightService` freezes the input bindings, plans metadata and record
requests, captures the target fingerprint and snapshot, performs offline
classification, and publishes the report and execution snapshot atomically.
For file-source comparisons, the same publication transaction stores an
`ExecutionPreviewSummary`. This scalar projection contains only the current
preflight identifier, snapshot and target bindings, disposition totals,
blocker totals, credential identity hashes, and execution-shape readiness.
Shared navigation reads this projection; it never opens the execution snapshot
or comparison manifest. The projection is display-only and cannot authorize a
load.
For a nullable self-referencing identity scope, `plan_preflight_requirements`
builds one exact name-and-lineage domain expression for each prepared record.
The expression ends with an unset parent at the root, and the planner batches
the expressions without widening the Odoo read.

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

The **Needs attention** projection is a manifest-authoritative action queue.
The pure `review_workbook_action_priority` function maps errors to **Must fix**
and warnings to **Review**; the workbook does not promote safe transformations
or blank cells into action items. The adapter shows the exact final prepared
field or relationship value when the frozen file-source evidence contains it,
then sorts all blockers before warnings. It indexes decisions by source
coordinate, prepared records by trace, source-issue keys, and target models
once before projecting rows. Workbook size therefore does not introduce a
repository, Odoo, or repeated full-collection lookup for each action item.

Traceability uses the accepted logical dataset and source row. The current
browser-compiled plan uses a synthetic contained source filename, so the
workbook does not present that value as the operator's original uploaded file
name.

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

For a Recipe application, the summary and execution preview resolve the shared
run credential through `WebContext.target_credential_workspace`. They retain
the application's current target details and use the verified setup workspace
identity only to address its vault entry. They do not open the setup workspace
store inside a request authorized for the application workspace. This keeps a
saved comparison readable and prevents an available shared key from being
misreported as missing. Reconnection still verifies unchanged schema and access
meaning before a new comparison can use the replacement credential generation.

### Reviewed deferred record groups

For a file-source report with isolatable blockers,
`PreflightService.deferred_scope_review` derives root issues and complete
affected-record closure from the saved manifest, full execution snapshot, and
frozen prepared dependencies. `preview_deferred_scope` keeps identity parents,
children, and required siblings together and reconciles prepared, omitted,
remaining-write, and remaining-problem counts. Exact numeric-precision issues
are calculated against the intended write rows and captured Odoo digits; the
path never rounds a value implicitly.

The browser renders the local preview through
`workspace_deferred_scope.html`. Acceptance posts the comparison identifier,
preview hash, and selected issue identifiers. The route recalculates the
preview before `PreflightService.accept_deferred_scope` publishes a compact
decision, reduced execution snapshot, and compact execution projection. A
stale comparison or changed preview is rejected. Preview, acceptance,
navigation, and workbook generation make no Odoo request.

`reduce_execution_snapshot` removes only the reviewed closure and rebuilds the
remaining relationship schedule. The result is loadable only when no blocker
remains and at least one safe write remains. The full comparison stays
immutable. The workbook's **Deferred issues** sheet projects every omitted
prepared row with its direct or inherited cause. The detailed invariants are
defined by the [reviewed deferred record groups contract](../contracts/deferred-record-groups.md).

## Code references

| Role | Code |
| --- | --- |
| Comparison orchestration | [`PreflightService`](../../../src/impodo/application/preflight_service.py) |
| Prepared-quality cause presentation | [`_quality_cause_groups`](../../../src/impodo/web/presenters/summary.py) |
| Compact execution projection | [`navigation.py`](../../../src/impodo/application/workspace/execution/navigation.py) and [`PreflightRepository`](../../../src/impodo/adapters/duckdb/preflight_repository.py) |
| Bounded shared navigation | [`WorkspaceNavigationQueryService`](../../../src/impodo/application/workspace/navigation.py) and [`WorkspaceNavigationRepository`](../../../src/impodo/adapters/duckdb/navigation_repository.py) |
| Background comparison control | [`preflight_jobs.py`](../../../src/impodo/application/preflight_jobs.py) |
| Bounded requirement planning | [`planner.py`](../../../src/impodo/domain/execution/planner.py) |
| Protected Odoo comparison | [`odoo_comparison_service.py`](../../../src/impodo/application/odoo_comparison_service.py) |
| Protected comparison contract | [`odoo_comparison.py`](../../../src/impodo/domain/odoo_comparison.py) |
| Frozen input | [`frozen_input.py`](../../../src/impodo/domain/preflight/frozen_input.py) |
| Review reports | [`reports.py`](../../../src/impodo/domain/preflight/reports.py) |
| Deferred-group closure and precision | [`preview_deferred_scope`](../../../src/impodo/domain/preflight/deferred_scope.py) |
| Reduced execution snapshot | [`reduce_execution_snapshot`](../../../src/impodo/domain/preflight/deferred_execution.py) |
| Deferred review and acceptance | [`PreflightService.deferred_scope_review`](../../../src/impodo/application/preflight_service.py) and [`PreflightService.accept_deferred_scope`](../../../src/impodo/application/preflight_service.py) |
| Workbook projection | [`reporting.py`](../../../src/impodo/adapters/artifacts/reporting.py) |
| Browser routes | [`preflight.py`](../../../src/impodo/web/routers/preflight.py) |
| Affected-group review page | [`workspace_deferred_scope.html`](../../../src/impodo/web/templates/workspace_deferred_scope.html) |
| Progress page | [`workspace_preflight_progress.html`](../../../src/impodo/web/templates/workspace_preflight_progress.html) |
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

Synchronous summary rendering, full execution-preview construction, and
fallback comparison rendering must run outside the event loop. Overview runs
its complete owner, navigation, and template build in the bounded thread pool.
Shared navigation uses only compact relational facts; full execution-preview
construction remains on the owning load pages. These containment rules keep
health and progress endpoints responsive without weakening the full snapshot
validation at load submission.

An ordinary Authoring comparison does not materialize the full execution
preview again after publication. The browser loads it when **Check changes**
opens. The post-comparison preview remains required for a Recipe Test
application because that path can prove that no load is needed and complete
through read-back automatically.

Workbook creation may load the complete eligible prepared set once because the
XLSX output contains one review row per decision. It may also load the complete
frozen normalization effect ledger once to explain those cells. Keep both
loads bounded to the exact current run. Index source traces and field effects
once in memory. The action queue must also index decisions, source issues, and
dataset target models once rather than repeatedly scanning those collections.
Do not reopen repositories for individual workbook rows or cells, and do not
contact Odoo while writing them.

## Verification

- [`tests/application/workspace/review/test_preflight.py`](../../../tests/application/workspace/review/test_preflight.py)
- [`tests/application/workspace/review/test_preflight_jobs.py`](../../../tests/application/workspace/review/test_preflight_jobs.py)
- [`tests/domain/preflight/test_review_workbook.py`](../../../tests/domain/preflight/test_review_workbook.py)
- [`tests/domain/preflight/test_deferred_scope.py`](../../../tests/domain/preflight/test_deferred_scope.py)
- [`tests/domain/preflight/test_deferred_execution.py`](../../../tests/domain/preflight/test_deferred_execution.py)
- [`tests/performance/test_preflight_scale.py`](../../../tests/performance/test_preflight_scale.py)
- [`tests/performance/test_deferred_scope_scale.py`](../../../tests/performance/test_deferred_scope_scale.py)
- [`tests/integration/artifacts/test_reporting_cli.py`](../../../tests/integration/artifacts/test_reporting_cli.py)
- [`tests/integration/artifacts/test_preflight_outputs.py`](../../../tests/integration/artifacts/test_preflight_outputs.py)
- [`tests/integration/odoo/test_connectors.py`](../../../tests/integration/odoo/test_connectors.py)
- [`tests/application/workspace/review/test_odoo_comparison.py`](../../../tests/application/workspace/review/test_odoo_comparison.py)
- [`tests/integration/web/test_review_workflow.py`](../../../tests/integration/web/test_review_workflow.py)
- [`tests/integration/web/test_summary_presenter.py`](../../../tests/integration/web/test_summary_presenter.py)
- [`tests/integration/web/test_deferred_scope_presenter.py`](../../../tests/integration/web/test_deferred_scope_presenter.py)
- [`tests/integration/web/test_deferred_scope_routes.py`](../../../tests/integration/web/test_deferred_scope_routes.py)
- [`tests/integration/duckdb/test_preflight_repository.py`](../../../tests/integration/duckdb/test_preflight_repository.py)
- [`tests/integration/duckdb/test_navigation_repository.py`](../../../tests/integration/duckdb/test_navigation_repository.py)
- [`tests/architecture/test_workspace_navigation_boundaries.py`](../../../tests/architecture/test_workspace_navigation_boundaries.py)
- [Recipe comparison and shared-key recovery](../../../tests/integration/web/test_recipe_comparison_recovery.py)
- [Nullable hierarchy matching and order](../../../tests/domain/preparation/test_target_first_relationships.py)

Verify fixed classification precedence, batched requests, portable identities,
snapshot completeness, stale bindings, deterministic artifacts, and absence of
write capabilities.

## Related documentation

- [User guide: Final review](../../user/workflow/05-final-review.md)
- [Preflight contract](../contracts/preflight.md)
- [Quality and quarantine contract](../contracts/quality-and-quarantine.md)
- [Reviewed deferred record groups contract](../contracts/deferred-record-groups.md)
- [Architecture decisions](../../decisions/README.md)
- [Recipe and data-version lifecycle contract](../contracts/recipe-lifecycle.md)
