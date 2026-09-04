---
audience: developer
stage: load
status: current
---

# Load into Odoo

## Responsibility

Load into Odoo validates the current execution scope, journals every planned
write, executes the reviewed dependency order through a write-only capability,
and reconciles affected records through a separate read capability.

It does not provide a generic Odoo client or whole-migration rollback.

## Entry conditions

The current final-review report must be `READY`; its execution snapshot,
preflight hash, mapping, target fingerprint, writable fields, and dependency
order must still match. The target must be explicitly allowed for the practical
rehearsal path and the actor must provide or have already stored the required
write-role credential.

## Odoo-to-Odoo transfer boundary through Stage 8B

The Odoo-source Authoring variant binds exactly two credentials. `SOURCE_FETCH`
captures and freezes the source. `DESTINATION_TRANSFER` is the second and final
key and belongs only to the different Odoo 19 destination. The destination key
is used for matching, preflight, confirmed loading, and read-back; the source
key is never substituted for it and no third credential role is introduced.

`DestinationMatchingService` performs bounded metadata and natural-key reads
for every frozen source model. It resolves generic many-to-one and many-to-many
evidence and normalizes inverse one-to-many metadata to the writable
many-to-one field. `TransferOrderService` derives dependency waves, while
`TransferReviewService` freezes create and update counts, write fields,
relationship operations, later relationship passes, and control totals.
Approval authorizes only that immutable package; it does not authorize
transport.

Stage 8A starts at `POST /workspaces/{workspace_id}/transfer-preflight`. The
route resolves the `DESTINATION_TRANSFER` vault entry and calls only the
read-identity probe and bounded destination readers. `TransferPreflightService`
compares the fresh aggregate result with the exact approved package. A changed
permission or company context, model or field scope, create or update
classification, record identity, or relationship resolution produces immutable
blocker evidence. The route never constructs `OdooWriteExecutor`, creates no
journal, and writes no Odoo record.

Stage 8B is deliberately split in two. `POST .../transfer-load/prepare` repeats
the destination read with the same transfer key, saves that fresh preflight,
and stops on drift. When it is ready, `TransferExecutionService` combines the
frozen Parquet tables, protected source relationship evidence, exact
destination record snapshot, approved transfer order, and preflight into an
`ExecutionSnapshot`. It expresses existing relations as business references,
incoming relations as logical references, preserves approved dependency waves,
and stages the snapshot as a no-write confirmation artifact.

The implementation is owned by
[`TransferExecutionService`](../../../src/impodo/application/transfer_execution_service.py).

`POST .../transfer-load` is the separate write boundary. Its form must carry
the current workspace revision, preflight hash, and execution-snapshot hash.
Only then does the background job re-probe the same destination key through
both the read-identity and exact write-scope interfaces, require the same
principal and company context, construct the writer, and call
`ExecutionService.execute_transfer`.

Immediately before journaling, execution bulk-revalidates every existing
record binding and proves that every key classified for creation is still
absent. Creates use deterministic External IDs. The repository transaction
requires the current ready transfer-preflight hash, workspace, and target,
then saves every planned attempt before transport. The shared dependency
engine performs creates, updates, and deferred relationship completion in the
approved order. The job attempts automatic read-back through
`ReconciliationService`; its outcome page also permits manual verification
with the same transfer key.

Changing destination identity, matching, order, package, approval, source, or
schema makes the saved evidence stale. A durable transfer journal disables a
second submission. If a process interruption leaves that journal `RUNNING`,
`POST .../transfer-load/recover` first uses
`ReconciliationService.assess_recovery` with the same destination key. It
constructs the writer only after the assessment succeeds and delegates the
same-run continuation to `TransferExecutionService.resume`. A terminal
`OUTCOME_UNKNOWN` transfer must be reconciled and must never be blindly
resubmitted.

## Implementation flow

For prepared-data workspaces, `execution.py` renders the preview, accepts the
hash-bound confirmation, builds the scoped executor, invokes
`ExecutionService.execute`, and exposes reconciliation and fallout routes.

The route resolves only the exact target-bound `WRITE` vault entry. Target
setup may have created that entry from the same operator-approved secret as the
`READ` entry, but execution never substitutes the read role. If no write entry
exists, the confirmation page requires a key approved for loading. Remote
execution probes the selected key against the exact reviewed readable and
writable model scope before writer construction.

`ExecutionService` validates the workspace evidence and API scope, bulk-checks
the frozen existing-target crosswalk, starts a durable run, records planned
rows before write transport, executes datasets in dependency order, and
records row-level results. Before every Odoo call, it also persists the exact
component, component page, transport batch, and operation phase as
`IN_FLIGHT`. If an outcome becomes unknown, the service stops before sending
later writes. `ReconciliationService` then reads back the affected scope and
publishes a separate reconciliation run.

For a verified Authoring load, the execution route also asks
`CorrectionWorkflowService.publish_completed_load` to join the current mapping,
prepared snapshots, execution snapshot, execution journal, reconciliation, and
protected target snapshot. `CorrectionOriginPublisher` then publishes one
whole-artifact origin and exact-target index and atomically closes the original
run and workspace. Failure to publish this optional successor evidence never
changes a verified Odoo outcome into a failed load; no correction action is
shown until the protected binding exists.

## Focused completed-load correction

`corrections.py` presents one run-owned successor journey. A stale mapping URL
for the closed original workspace redirects to this explanation; repository
immutability remains the authoritative guard. `CorrectionSuccessorService`
creates one restart-safe Authoring run and workspace over the same DataVersion,
copies the exact target setup, and seeds the prior mapping as a working draft.

**Review correction** runs in `CorrectionJobManager`, so page reloads reuse the
one active attempt. `CorrectionAuthoringStageCoordinator` checks and submits
the current mapping, invokes the existing preparation and quality owners,
freezes the prepared review, and resolves one current read capability.
`NativeCorrectionReviewPipeline` scans the previous and corrected prepared
Parquet artifacts through Polars, emits only sparse A/C candidates, and reads B
from Odoo only by protected exact identifiers. Browser projections receive
only counts and blocker messages; numeric identifiers, field values,
credentials, and evidence hashes remain protected.

Formula-like transformations, Selection choices, constants, fallbacks,
source-field choices, and casing all produce the same corrected-intent
comparison when their scalar output changes. Exact-existing many-to-one
changes use the same three-way decision after `CorrectionReviewService`
resolves each distinct previous and corrected business key once. It submits at
most 20 exact keys in one `find_records_many` call, requires one case-sensitive
match, and caches that protected identity for every affected candidate row.
Missing, duplicate, unsupported, and changed-field matches block the whole
plan.

The protected plan stores only the resolved relationship IDs needed by the
parent field. Apply does not search again. Its scoped writer can update the
parent model and field only; the related model receives no create or update
capability.

**Apply N corrections** requires an explicit checkbox and a separately resolved
write credential. The route probes the narrow write scope, publishes a
confirmation, probes again immediately before transport, and delegates to
`CorrectionExecutionService`. That service rereads current values, journals
before bounded exact-ID updates, and starts exact-ID reconciliation
automatically. A verified result closes the successor run and workspace.

## Current relationship ordering

`extract_dataset_dependency_edges` derives every dataset dependency from
compiled identity, scope, and relationship resolvers. The same immutable edge
evidence is used by browser and profile validation, compilation, preflight,
and `build_execution_snapshot`. The preflight requirement plan records the
hard and deferrable edges in its semantic hash.
`dependency_ordered_execution_datasets` calculates strongly connected dataset
components, places every acyclic dependency component before its consumers,
and retains reviewed order only inside a component. The resulting order and
dependency list contribute to the `ExecutionSnapshot` semantic hash.

`plan_execution_rows` then resolves each incoming relationship through the
frozen source business-key index. Each actionable row receives a deterministic
ordinal and component. Each incoming relational field records its resolved
dependency row identifiers and its hard or deferrable strength.

The iterative row scheduler treats an existing unique target as already
satisfied and a new target as satisfied only after its create receipt. It
orders acyclic references, including same-dataset parent hierarchies, without
a relationship patch. When optional edges form a cycle, it omits only the
exact planned owner fields and records those fields in the snapshot completion
list. A hard-edge row cycle or an unusable incoming target creates snapshot
blocker evidence and prevents the execution journal and Odoo transport from
starting.

`ExecutionService` consumes the frozen topological component layers through
pages of at most 500 rows. A page never mixes components. It groups only
dependency-independent rows by dataset and compatible create shape, journals
each create receipt, and requires that receipt before a retained dependency can
write. It then applies the snapshot's completion fields after the required
receipts exist. The journal keeps those rows `PARTIALLY_APPLIED` until
relationship completion succeeds.

When a reviewed incoming relationship targets a record that Odoo generates
from an earlier create, its field intent carries one captured read-only
many2one projection. After the direct source create is journalled, the service
reads that field in exact pages of at most 500 source identifiers and records
the projected model and identifier on the source attempt. The dependent row
uses Odoo's numeric relationship import only after that receipt is durable.
This covers a Product template whose generated variant is needed by a BOM line
without adding a Product-specific service path.

Before the journal starts, the service collects all existing row identities
and reviewed target relationship identities. `find_ids_many` resolves them in
model-grouped pages of at most 100 exact keys. The snapshot's opaque binding
hash proves that a still-unique key did not silently retarget to a different
Odoo record. Numeric Odoo IDs remain runtime-only. Failure returns to **Check
changes** without creating a run or sending an Odoo write.

The accepted [scalable relationship dependency
plan](../../plans/scalable-relationship-dependency-planning.md) now provides
immutable row-edge and schedule evidence, exact cycle classification, bounded
crosswalk revalidation, receipt-gated component execution, and read-back-gated
component recovery. The browser now derives bounded progressive guidance from
that same snapshot. The current 25,000-row Product/BOM macOS qualification and
the bounded Odoo 19 generated-variant execution probe pass; clean Windows and
browser-evidence gates remain.

## Browser guidance and progress

`ExecutionService.current_preview` projects the immutable relationship plan
into at most five visible load groups. Each group contains only its sequence,
record count, and at most three prepared-data labels. It also groups equivalent
snapshot blockers into at most five plain-language categories with a record
count, bounded record-type labels, and one next action. These projections do
not recalculate ordering, carry row identifiers into the template, or become
execution authority.

The review page explicitly says that the order follows the current mappings,
included rows, and optional relationships. It is therefore safe for a complex
BOM while preserving the data manager's ability to change those choices and
compare again. Exact hashes and bounded plan counts remain collapsed under
**Support details**.

`LoadJobManager` derives the current load-group position and relationship
totals from journalled row states. `PLANNED`, `IN_FLIGHT`, and `RETRY_READY`
rows keep first-pass progress open. `PARTIALLY_APPLIED` rows keep relationship
progress open. The browser does not call any of those states complete, and it
does not enter relationship completion until every first-pass write has a
recorded outcome.

## Code references

| Role | Code |
| --- | --- |
| Execution orchestration | [`ExecutionService`](../../../src/impodo/application/workspace/execution/service.py) |
| Background load jobs | [`LoadJobManager`](../../../src/impodo/application/workspace/execution/load_jobs.py) |
| Browser load guidance | [`ExecutionPreview`](../../../src/impodo/application/workspace/execution/service.py) |
| Browser load-job contract | [`LoadJob`](../../../src/impodo/application/workspace/execution/job_models.py) |
| Execution snapshot | [`execution_snapshot.py`](../../../src/impodo/domain/execution_snapshot.py) |
| Dataset dependency order | [`dependency_ordered_execution_datasets`](../../../src/impodo/domain/execution_snapshot.py) |
| Row dependency scheduling | [`dependency_scheduler.py`](../../../src/impodo/domain/execution/dependency_scheduler.py) |
| Bounded component paging | [`dependency_component_pages`](../../../src/impodo/domain/execution/dependency_scheduler.py) |
| Snapshot row-plan construction | [`plan_execution_rows`](../../../src/impodo/domain/execution_snapshot.py) |
| Odoo identity lookup contract | [`odoo_write.py`](../../../src/impodo/domain/execution/odoo_write.py) |
| JSON-2 bulk crosswalk and projected-receipt adapter | [`writer.py`](../../../src/impodo/adapters/odoo/writer.py) |
| Canonical dependency evidence | [`relationship_dependencies.py`](../../../src/impodo/domain/relationship_dependencies.py) |
| Required dependency validation | [`dependencies.py`](../../../src/impodo/domain/mapping/validation/dependencies.py) |
| Journal states | [`execution/models.py`](../../../src/impodo/domain/execution/models.py) |
| Reconciliation | [`ReconciliationService`](../../../src/impodo/application/workspace/execution/reconciliation.py) |
| Recovery read-back | [`ReconciliationService.assess_recovery`](../../../src/impodo/application/workspace/execution/reconciliation.py) |
| Read-back-gated resume | [`ExecutionService.resume`](../../../src/impodo/application/workspace/execution/service.py) |
| Odoo-transfer resume facade | [`TransferExecutionService.resume`](../../../src/impodo/application/transfer_execution_service.py) |
| Hash-bound Odoo-transfer resume | [`ExecutionService.resume_transfer`](../../../src/impodo/application/workspace/execution/service.py) |
| Durable batch and recovery transitions | [`ExecutionRepository`](../../../src/impodo/adapters/duckdb/execution_repository.py) |
| Browser routes | [`execution.py`](../../../src/impodo/web/routers/execution.py) |
| Correction browser orchestration | [`CorrectionWorkflowService`](../../../src/impodo/application/correction_workflow.py) |
| Resumable correction jobs | [`CorrectionJobManager`](../../../src/impodo/application/correction_jobs.py) |
| Correction origin and review owners | [`correction_orchestration.py`](../../../src/impodo/application/correction_orchestration.py) |
| Exact-target and relationship correction review | [`CorrectionReviewService`](../../../src/impodo/application/correction_service.py) |
| Protected exact-ID correction execution | [`CorrectionExecutionService`](../../../src/impodo/application/correction_execution.py) |
| Native sparse review pipeline | [`NativeCorrectionReviewPipeline`](../../../src/impodo/adapters/correction_review_pipeline.py) |
| Polars and Parquet sparse reduction | [`write_polars_correction_candidates`](../../../src/impodo/adapters/polars_correction.py) |
| Odoo-to-Odoo destination matching | [`DestinationMatchingService`](../../../src/impodo/application/destination_matching_service.py) |
| Odoo-to-Odoo relationship order | [`TransferOrderService`](../../../src/impodo/application/transfer_order_service.py) |
| Odoo-to-Odoo review package | [`TransferReviewService`](../../../src/impodo/application/transfer_review_service.py) |
| Odoo-to-Odoo read-only preflight | [`TransferPreflightService`](../../../src/impodo/application/transfer_preflight_service.py) |
| Transfer evidence publication and invalidation | [`WorkspaceStateService`](../../../src/impodo/domain/workspace/workbench.py) |

## Evidence and state

The execution snapshot is semantic-hash bound. `ExecutionRun` and
`ExecutionRowAttempt` distinguish planned, in-flight, retry-ready, committed,
partially applied, failed, blocked, and outcome-unknown states. A partially
applied create can retain immutable projected Odoo receipts as well as its
direct identifier. The attempt record retains the active component and batch
after a process restart without adding a parallel recovery store. Final reconciliation is new evidence and
does not rewrite the journal. A recovery assessment remains unpublished;
execution atomically records its semantic hash on every row before resume.
The compact browser summary and job counters are disposable projections of
this evidence. They do not alter the snapshot or journal contract.

## Completion and navigation

No-change previews complete without transport. A write run completes only when
the journal has no unknown outcome and reconciliation verifies the expected
target state. Navigation reports **Verify outcome** or **Needs attention** when
the write result is not yet proven.

Successful reconciliation remains evidence of this Project-owned DataVersion
and run. It does not publish or qualify a Recipe. A future application of
published rules must create its own run, comparison, execution, and
reconciliation evidence and cannot inherit this write authority. That workflow
belongs to the integrated Test workflow.

## Invalidation and recovery

Fail closed when any snapshot or scope hash differs. On
`OdooWriteOutcomeUnknown`, journal the affected batch, stop later writes, and
require reconciliation before any new action. Do not convert a connection
reset or wrapped HTTP 422 into a safe-to-retry failure.

After a process interruption, `ReconciliationService.assess_recovery` checks
all committed, in-flight, partially applied, and not-yet-started rows against
the immutable schedule. `ExecutionService.resume` handles a prepared-data
load, while `ExecutionService.resume_transfer` additionally requires the
current transfer preflight, exact staged snapshot, original destination-key
binding, and matching read and write identity. Each path reuses an already
recorded recovery report after another restart or records a new report
atomically. It retries only a create proven absent, an exact update whose
reviewed fields still differ, or the frozen deferred fields of a created row.
It verifies all earlier committed components first and revalidates the target
crosswalk before transport resumes. Transfer creates repeat their absence
check and retain their original External IDs.

Deferred relationships are applied only after their dependencies exist. A
partial relationship outcome remains explicit and recoverable through the
journal.

If interruption occurs between a direct create and generated-record read-back,
resume reuses the direct journal receipt and repeats only the exact projection
read. It never sends the source create again. A missing or changed projection
blocks the dependent component.

## Odoo 19 and performance

Remote writes use the Odoo 19 JSON-2 boundary with named, scoped operations.
Creates are grouped by compatible field shape and sent in bounded batches.
Existing-row and target-relationship identities are resolved in bounded bulk
queries; relationship count therefore does not create per-row lookup traffic.
Ordinary load updates still call `update_row` once per changed record. The
completed-load correction path may group up to 50 exact IDs only when
their sparse field payload is identical. Every affected row is journalled
`IN_FLIGHT` before that shared call, and an unknown batch outcome remains
unknown for every included row until automatic exact-ID reconciliation.

Read-back reconciliation batches by model and exact requested field scope.
Different field sets for the same model do not force one broad union read.
Keep write and read interfaces separate so a nominally read-only component
cannot invoke a write method.

The `PRODUCTION` DataVersion purpose does not bypass the current
disposable-target acceptance boundary. Recipe lineage is not Odoo write
authorization.

### Completed-load correction qualification

The opt-in
[`qualify_completed_load_correction.py`](../../../scripts/qualify_completed_load_correction.py)
runner accepts only a database whose name begins with
`impodo_correction_`. It uses the production Polars sparse comparison,
`CorrectionReviewService`, `CorrectionExecutionService`, the closed JSON-2
reader and writer, and automatic reconciliation. A separate fixed setup and
cleanup seam creates and deletes synthetic Products; it cannot grant Unit
writes to the correction scope.

The 2026-08-30 local Odoo 19 qualification verified 768 scalar and 37
exact-existing many-to-one Product fields. Calls scaled by 50-ID pages and two
distinct Unit keys rather than by one Unit lookup per Product. The repeat
review proposed no write, and the conflict, known-rejection, and lost-response
cases remained fail-closed.

The hosted Odoo Online 19 rerun verified the same 805 fields over HTTPS in
100.833865 seconds. An initial reconciliation timeout published no verified
result and cleaned every fixture record. `Json2ReadbackReader` uses the
configured bounded retry for safe exact reads; the writer never retries an
uncertain write.

This evidence qualifies the current literal-loopback and hosted Odoo Online
19 Product boundary. It does not expand the Authoring-only correction scope or
qualify another remote topology.

## Verification

- [`tests/application/workspace/execution/test_service.py`](../../../tests/application/workspace/execution/test_service.py)
- [`tests/integration/web/test_execution.py`](../../../tests/integration/web/test_execution.py)
- [`tests/integration/duckdb/test_execution_repository.py`](../../../tests/integration/duckdb/test_execution_repository.py)
- [`tests/application/workspace/execution/test_load_jobs.py`](../../../tests/application/workspace/execution/test_load_jobs.py)
- [`tests/domain/execution/test_snapshot.py`](../../../tests/domain/execution/test_snapshot.py)
- [`tests/domain/execution/test_dependency_scheduler.py`](../../../tests/domain/execution/test_dependency_scheduler.py)
- [`tests/domain/recipe/test_profile_and_values.py`](../../../tests/domain/recipe/test_profile_and_values.py)
- [`tests/domain/test_relationship_dependencies.py`](../../../tests/domain/test_relationship_dependencies.py)
- [`tests/application/workspace/execution/test_reconciliation.py`](../../../tests/application/workspace/execution/test_reconciliation.py)
- [`tests/integration/odoo/test_readback_retries.py`](../../../tests/integration/odoo/test_readback_retries.py)
- [`tests/integration/web/test_load_workflow.py`](../../../tests/integration/web/test_load_workflow.py)
- [`tests/integration/web/test_correction_workflow.py`](../../../tests/integration/web/test_correction_workflow.py)
- [`tests/application/test_correction_jobs.py`](../../../tests/application/test_correction_jobs.py)
- [`tests/application/test_correction_orchestration.py`](../../../tests/application/test_correction_orchestration.py)
- [`tests/application/test_correction_execution.py`](../../../tests/application/test_correction_execution.py)
- [`tests/integration/columnar/test_polars_correction.py`](../../../tests/integration/columnar/test_polars_correction.py)
- [`tests/performance/test_correction_qualification.py`](../../../tests/performance/test_correction_qualification.py)
- [`tests/application/workspace/test_destination_matching.py`](../../../tests/application/workspace/test_destination_matching.py)
- [`tests/application/workspace/test_transfer_order.py`](../../../tests/application/workspace/test_transfer_order.py)
- [`tests/application/workspace/test_transfer_review.py`](../../../tests/application/workspace/test_transfer_review.py)
- [`tests/application/workspace/test_transfer_preflight.py`](../../../tests/application/workspace/test_transfer_preflight.py)
- [`tests/application/workspace/test_transfer_execution.py`](../../../tests/application/workspace/test_transfer_execution.py)
- [`tests/integration/web/test_transfer_order_navigation.py`](../../../tests/integration/web/test_transfer_order_navigation.py)
- [`tests/integration/web/test_transfer_review_routes.py`](../../../tests/integration/web/test_transfer_review_routes.py)
- [`tests/integration/web/test_transfer_preflight_routes.py`](../../../tests/integration/web/test_transfer_preflight_routes.py)
- [`tests/integration/web/test_transfer_load_routes.py`](../../../tests/integration/web/test_transfer_load_routes.py)
- [`tests/integration/duckdb/test_transfer_order_persistence.py`](../../../tests/integration/duckdb/test_transfer_order_persistence.py)

Verify scope enforcement, dependency order, create batching, update behavior,
journal-before-transport, unknown outcomes, deferred relationships,
reconciliation, and repeat-preview safety against an explicitly disposable
Odoo 19 target.

## Related documentation

- [User guide: Load into Odoo](../../user/workflow/06-load-into-odoo.md)
- [Execution and reconciliation contract](../contracts/execution-and-reconciliation.md)
- [Security and infrastructure](../../architecture/security-and-infrastructure.md)
- [Acceptance and test strategy](../../testing/acceptance.md)
- [Remote Odoo 19 acceptance](../runbooks/remote-odoo-acceptance.md)
- [Recipe and data-version lifecycle contract](../contracts/recipe-lifecycle.md)
- [Proposed scalable relationship dependency plan](../../plans/scalable-relationship-dependency-planning.md)
