---
audience: developer
stage: integrated-test
status: current
---

# Integrated multi-Recipe Test run

## Responsibility

M4 plans and provisions one Project-owned Test `MigrationRun` over one frozen
Test `DataVersion`. It pins exact Recipe revisions, validates dependencies and
conservative field-level write ownership, stores one run-level target
projection, and creates one isolated `RecipeApplication` and
`MigrationWorkspace` per selected Recipe.

M4 creates fresh mapping drafts and focused readiness issues. M5 now binds the
run to a CutoverPlan revision immediately after provisioning. Planning does
not execute applications, publish qualification, or grant Production
authority.

## Entry conditions

The Project must own one accepted frozen Test DataVersion, at least one exact
protected Recipe revision, and an Authoring workspace with a reviewed live
Odoo 19 schema and any required supporting references. The current browser
does not yet create that Test package.

## Implementation flow

### Browser entry

`GET /projects/{project_id}/test-runs/new` reads bounded Project, DataVersion,
Recipe, run, and workspace registry projections. The data manager selects an
accepted Test DataVersion, an Authoring workspace containing reviewed live
Odoo 19 evidence, exact Recipe revisions, and any explicit dependency edges.

`POST /projects/{project_id}/test-runs/new` resolves the target workspace under
the same Project, verifies the current read-credential generation, and invokes
`MigrationRunPlanningService.start_test_run`. The result redirects to
`/projects/{project_id}/runs/{migration_run_id}`. The run page reads integrated
status and all application issues from the registry; it does not open every
application workspace.

### Planning and collision checks

`review_test_run` verifies one frozen Test package, Project ownership, exact
protected Recipe envelopes, physical source bindings, parameters, controls,
Odoo 19 compatibility, and supporting reference versions. It builds a
deterministic topological order and rejects missing nodes, duplicate edges,
self-dependencies, cycles, and incompatible versions of one named reference
dataset.

The first M4 write contract deliberately uses conservative ownership: two
selected Recipes may not both claim the same Odoo model and writable field.
There is no last-writer-wins or reordering escape. General record-domain merge
semantics remain out of scope.

### Shared target evidence

The planner unions all required Odoo models and fields, filters the reviewed
schema to that model set, and stores it once under the run. It similarly
stores only supporting reference datasets named by the selected Recipes.
`RunAwareSchemaRepository` and `RunAwareAdvancedCoverageRepository` project
only one application's requirements into its workspace and reject per-
application recapture.

The current M4 flow reuses one already reviewed live Authoring snapshot. It
makes no Odoo call per Recipe or source row. A later target refresh must remain
a run-owned batch operation; it must not reintroduce workspace-owned target
capture.

## Evidence and state

### Provisioning and recovery

One operation intent stores the canonical run, TargetBinding, requirement
plan, target schema, reference bundle, applications, workspaces, requirements,
and initial issues. One registry transaction creates all identities and
advances the Project revision once. Workspace stores are created afterward;
the intent remains pending until every compiler attempt is recorded.

Replaying the same operation after a registry or store fault reconstructs the
stored identities and does not add a run, target binding, application, or
workspace. A changed request under the same operation ID fails closed.

### Fresh compiler boundary

`RecipeApplicationService` adapts the retained Recipe application compiler to an
already provisioned application workspace. It never calls the superseded
Recipe-owned DataVersion or application-creation paths. It rebuilds governance
and structural preparation, rebinds logical source columns, creates a normal
mapping working draft, and stores a mapping-bound quality seed.

Portable `NORMALIZE_TEXT` preparation is compiled into the normal scalar
mapping transformation instead of materializing a copied source column.
Source, target, or reference blockers prevent an unsafe mapping. Reviewable
quality-scope or categorical blockers retain a fresh draft but keep the
application `BLOCKED`.

## Completion and navigation

The browser redirects to the integrated run page after provisioning. `READY`
means every application has a compatible fresh draft; `BLOCKED` retains the
owning issues and any safe draft. Neither state implies execution,
qualification, rollout selection, or Production authority. The run page links
to the separate M5 qualification review.

## Invalidation and recovery

Changing the DataVersion, Recipe selection or revision, dependency graph,
target evidence, reference versions, parameter values, controls, or credential
generation changes plan meaning and requires a new operation. Exact replay may
resume a faulted operation; changed meaning under the same operation ID is
rejected.

## Odoo 19 and performance

The current registry generation is `impodo-migration-registry-2026-08-m6`. Older
development storage is rejected rather than upgraded. The DataVersion and
MigrationWorkspace source stores remain on their exact M2 generations because
their ownership contracts did not change.

Provisioning reads one Project outside the application loop and indexes
workspaces by ID. Integrated progress and batch issues use bounded registry
queries. Per-application source projection and compiler writes are required
because mutable state is isolated; Odoo calls, Project lookups, and source-row
queries must not scale with Recipe count.

### Current limitation

M4 consumes an already accepted Test DataVersion. It does not add the browser
intake workflow that assembles and accepts a new Test package. Documentation
and UI must disclose that prerequisite rather than suggest that the Authoring
sample is valid Test evidence.

## Code references

| Role | Code |
| --- | --- |
| Domain plan and application state | [`migration_run_planning.py`](../../../src/impodo/migration_run_planning.py) |
| Planner and provisioning coordinator | [`MigrationRunPlanningService`](../../../src/impodo/application/migration_run_planning_service.py) |
| Fresh Recipe application service | [`RecipeApplicationService`](../../../src/impodo/application/recipe_application_service.py) |
| Registry and recovery | [`MigrationRunPlanningRepository`](../../../src/impodo/adapters/duckdb/migration_run_planning_repository.py) |
| Run-owned schema projection | [`RunAwareSchemaRepository`](../../../src/impodo/adapters/duckdb/run_aware_schema_repository.py) |
| Run-owned reference projection | [`RunAwareAdvancedCoverageRepository`](../../../src/impodo/adapters/duckdb/run_aware_advanced_coverage_repository.py) |
| Browser routes | [`integrated_runs.py`](../../../src/impodo/web/routers/integrated_runs.py) |

## Verification

- [`tests/test_migration_project_phase_m4_multi_recipe_runs.py`](../../../tests/test_migration_project_phase_m4_multi_recipe_runs.py)
- [`tests/test_migration_project_phase_m3_project_authoring.py`](../../../tests/test_migration_project_phase_m3_project_authoring.py)
- [`tests/test_migration_project_phase_m2_source_packages.py`](../../../tests/test_migration_project_phase_m2_source_packages.py)

## Related documentation

- [Data-manager guide](../../user/guides/integrated-test-runs.md)
- [Project lifecycle](../contracts/project-lifecycle.md)
- [Integrated run lifecycle contract](../contracts/integrated-run-lifecycle.md)
- [Evidence lifecycle](../contracts/evidence-lifecycle.md)
- [M4 implementation record](../../plans/migration-projects-phase-m4-multi-recipe-runs.md)
- [M5 qualification workflow](08-integrated-qualification.md)
