---
audience: developer
stage: integrated-qualification
status: current
---

# Integrated Test qualification

## Responsibility

M5 versions one Project-scoped `CutoverPlan`, qualifies exact Test evidence,
and records a separate Project rollout selection. It reuses the existing
mapping, preparation, comparison, execution, read-back, and reconciliation
pipeline in each isolated application workspace. It adds no second execution
engine and grants no Production authority.

## Entry conditions

The Project must own a frozen complete Test DataVersion, one M4 integrated
Test run, its exact plan binding, and one isolated application workspace per
selected Recipe revision. Every application must use the run target evidence.

## Implementation flow

### Plan revision boundary

`MigrationRunPlanningService` provisions the M4 run and then asks
`CutoverPlanRepository.ensure_for_run` to bind it. The stable Project plan ID
has immutable revisions. A revision pins selected Recipe revisions,
dependencies, conservative field-level write ownership, unioned requirement
meaning, and the two required Project controls.

The run ID, Test target, source rows, and timestamps are not reusable plan
meaning. An unchanged plan can therefore be exercised by another Test run.
A selected Recipe revision, dependency, requirement, write owner, or shared
control change appends a new unqualified revision.

## Evidence and state

### Qualification review

`WorkspaceIntegratedQualificationEvidenceReader` reads the current evidence
for each application only when the data manager opens the qualification page
or submits qualification. It makes no Odoo call. Each application must prove:

- exact submitted mapping and current canonical preparation;
- passing control totals and ready quality evidence;
- a current ready comparison and execution snapshot;
- a fully committed execution with no failed, partial, blocked, unknown, or
  planned write outcome; and
- verified read-back with no fallout or unknown outcome.

`CutoverPlanService.review` also proves the frozen Test package, exact shared
target binding, complete application membership, and dependency time order.
The normal integrated run page remains a bounded registry projection and does
not open application stores.

### Dependency write guard

Before the browser probes a write identity or constructs an Odoo writer,
`CutoverPlanService.assert_application_can_execute` checks every direct
predecessor. The predecessor must have current `VERIFIED` reconciliation with
no fallout or unknown outcome. The guard is repeated in the POST write path,
so bypassing the confirmation page cannot start the downstream write.

This is one registry read for application membership and one current
reconciliation read per declared predecessor. It performs no source-row query
and no Odoo request. Do not move it inside a row or batch loop.

### Protected persistence

Qualification uses deterministic child IDs under one operation intent.
Per-application and integrated payloads are canonical JSON encrypted with a
Project-scoped AES-256-GCM key in `.project-evidence-protected`. The registry
stores only identities, counts, hashes, storage keys, actor identity, time,
and status.

Evidence is written before the registry transaction. Exact replay authenticates
and reuses the same artifacts. The transaction publishes all application
qualifications and the integrated qualification together, marks applications
qualified, completes the Test run, advances the Project revision once, and
commits the intent.

Rollout selection is a separate restart-safe operation. It may select only a
qualification for the current plan revision. It does not populate a
Production run, target binding, credential, approval, or write command.

## Completion and navigation

The integrated run page shows bounded plan and qualification status and links
to the qualification review. `READY` means the evidence can be qualified.
`QUALIFIED` exposes the separate selection action. `SELECTED` records the
rollout candidate while still explaining the Production boundary.

## Invalidation and recovery

Changed plan meaning creates a new unqualified revision. Changed workspace
evidence makes a pending review fail its expected evidence hash. Exact replay
after protected-store or registry failure authenticates and reuses the same
artifacts; changed meaning under the same operation ID fails closed.

## Odoo 19 and performance

The exact registry generation is `impodo-migration-registry-2026-08-m5`.
M4 and older development storage are rejected rather than upgraded. The new
protected directory is an explicit clean-root member, and its shortened
content-bound filenames stay within normal Windows path limits.

Project lists, Project overviews, and integrated run pages read bounded
registry projections. Full qualification review intentionally opens each
selected application once. It must never add an Odoo call per Recipe, a
repository query per source row, or repeat application reads inside row loops.

## Code references

| Role | Code |
| --- | --- |
| Domain contracts | [`migration_cutover.py`](../../../src/impodo/migration_cutover.py) |
| Qualification service and evidence reader | [`cutover_plan_service.py`](../../../src/impodo/application/cutover_plan_service.py) |
| Registry and recovery | [`cutover_plan_repository.py`](../../../src/impodo/adapters/duckdb/cutover_plan_repository.py) |
| Protected evidence | [`protected_project_evidence_store.py`](../../../src/impodo/adapters/protected_project_evidence_store.py) |
| Browser and dependency guard | [`cutover_plans.py`](../../../src/impodo/web/routers/cutover_plans.py), [`execution.py`](../../../src/impodo/web/routers/execution.py) |

## Verification

- [`tests/test_migration_project_phase_m5_cutover_qualification.py`](../../../tests/test_migration_project_phase_m5_cutover_qualification.py)
- [`tests/test_migration_project_phase_m4_multi_recipe_runs.py`](../../../tests/test_migration_project_phase_m4_multi_recipe_runs.py)
- [`tests/test_execution_web.py`](../../../tests/test_execution_web.py)

## Related documentation

- [Data-manager guide](../../user/guides/qualify-integrated-test.md)
- [Cutover plan lifecycle contract](../contracts/cutover-plan-lifecycle.md)
- [Integrated Test run contract](../contracts/integrated-run-lifecycle.md)
- [Execution and reconciliation contract](../contracts/execution-and-reconciliation.md)
- [M5 implementation record](../../plans/migration-projects-phase-m5-cutover-qualification.md)
