---
audience: developer
stage: integrated-test
status: current
---

# Integrated multi-Recipe Test run

## Responsibility

This workflow plans and provisions one Project-owned Test `MigrationRun` over one frozen
Test `DataVersion`. It pins exact Recipe revisions, validates dependencies and
conservative field-level write ownership, stores one run-level target
projection, and creates one isolated `RecipeApplication` and
`MigrationWorkspace` per selected Recipe.

Planning creates fresh mapping drafts and focused readiness issues, then binds the
run to a CutoverPlan revision immediately after provisioning. Planning does
not execute applications, publish qualification, or grant Production
authority.

## Entry conditions

The Project must own at least one exact protected Recipe revision from an
accepted Authoring DataVersion. The browser then creates a fresh draft Test
DataVersion, Test MigrationRun, and shared setup MigrationWorkspace before it
accepts source or target evidence.

## Implementation flow

### Browser entry

`GET /projects/{project_id}/test-runs/new` reads the Project and its bounded
Recipe projection. The data manager selects exact Recipe revisions, any
explicit dependency edges, and the newer delivery cutoff.

`POST /projects/{project_id}/test-runs/new` invokes
`TestRunSetupService.start_setup`. One restart-safe operation creates the draft
Test DataVersion, draft Test MigrationRun, shared setup MigrationWorkspace,
source package, and `TestRunSetupBinding`. The binding pins exact Recipe
semantic hashes and dependency order before the browser redirects to source
upload.

When the data manager saves the Test target, the target route reads the setup
binding and preselects the union of models declared by the selected Recipe
revisions. It does not call Odoo once per Recipe. The normal schema capture
performs the bounded Odoo 19 metadata read.

`GET` and `POST /projects/{project_id}/test-runs/{migration_run_id}/activate`
review and activate the same run. Activation requires the frozen Test package,
live target evidence from the shared setup workspace, and the current
read-credential generation. `MigrationRunPlanningService.activate_test_run`
then stores the run target and plan atomically and creates one isolated
application workspace per selected Recipe. The integrated run page continues
to read bounded registry status without opening every application workspace.

### Planning and collision checks

`review_test_run` verifies one frozen Test package, Project ownership, exact
protected Recipe envelopes, physical source bindings, parameters, controls,
Odoo 19 compatibility, and supporting reference versions. It builds a
deterministic topological order and rejects missing nodes, duplicate edges,
self-dependencies, cycles, and incompatible versions of one named reference
dataset.

The write contract deliberately uses conservative ownership: two
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

The current flow captures one fresh live snapshot of the Odoo target chosen
for the Test run in the shared setup workspace. It makes no Odoo call per
Recipe or source row. A later target refresh remains a run-owned batch
operation and must not reintroduce application-workspace target capture.

## Evidence and state

### Provisioning and recovery

The setup operation uses deterministic child identities for the DataVersion,
run, setup workspace, and selection binding. The later activation operation
stores the TargetBinding, requirement plan, target schema, reference bundle,
applications, application workspaces, requirements, and initial issues. One
activation transaction advances the Project revision. Application workspace
stores are created afterward; the activation intent remains pending until
every compiler attempt is recorded.

Replaying either operation after a registry or store fault reconstructs its
stored identities and does not add a data version, run, target binding,
application, or workspace. A changed request under the same operation ID
fails closed.

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
to the separate qualification review.

Workspace ownership selects one browser journey through
`classify_workspace_journey`. An Authoring workspace keeps the six-stage
navigation. A Test setup workspace exposes only **Fresh data**, **Check Odoo**,
and the activation review. A `RecipeApplication` workspace exposes only the
preparation, review, load, and verification routes grouped under **Review and
load**. `WorkspaceAccessMiddleware` applies this policy after resolving the
verified workspace lineage and before a route opens child state. A stale GET or
POST for an incompatible workspace area redirects to the owning run without
executing that route.

The run page enters an application through
`GET /projects/{project_id}/runs/{migration_run_id}/applications/{application_id}`.
Odoo recovery enters the one shared setup workspace through
`GET /projects/{project_id}/runs/{migration_run_id}/odoo`. These run-owned
routes prevent application pages from becoming a second Authoring workflow.

## Invalidation and recovery

Changing the DataVersion, Recipe selection or revision, dependency graph,
target evidence, reference versions, parameter values, controls, or credential
generation changes plan meaning and requires a new operation. Exact replay may
resume a faulted operation; changed meaning under the same operation ID is
rejected.

## Odoo 19 and performance

The current implementation accepts Odoo 19. Supporting a later Odoo major
version requires extending and testing the compatibility policy. The browser
describes the selected destination as a supported Odoo target so its labels do
not need to change for each newly supported major version.

The exact registry generation is
`impodo-migration-registry-2026-08-project-root`. The DataVersion generation is
`impodo-data-version-store-2026-08-project-owned`; the MigrationWorkspace
reference-store generation is
`impodo-migration-workspace-2026-08-reference-only`. Supported older versions
within those generations upgrade transactionally before use. Other
generations remain unchanged and fail closed.

Provisioning reads one Project outside the application loop and indexes
workspaces by ID. Integrated progress and batch issues use bounded registry
queries. Per-application source projection and compiler writes are required
because mutable state is isolated; Odoo calls, Project lookups, and source-row
queries must not scale with Recipe count.

## Code references

| Role | Code |
| --- | --- |
| Domain plan and application state | [`migration_run_planning.py`](../../../src/impodo/migration_run_planning.py) |
| Test setup binding | [`migration_test.py`](../../../src/impodo/migration_test.py) |
| Test setup coordinator | [`TestRunSetupService`](../../../src/impodo/application/test_run_setup_service.py) |
| Planner and provisioning coordinator | [`MigrationRunPlanningService`](../../../src/impodo/application/migration_run_planning_service.py) |
| Fresh Recipe application service | [`RecipeApplicationService`](../../../src/impodo/application/recipe_application_service.py) |
| Registry and recovery | [`MigrationRunPlanningRepository`](../../../src/impodo/adapters/duckdb/migration_run_planning_repository.py) |
| Test setup persistence | [`TestRunRepository`](../../../src/impodo/adapters/duckdb/test_run_repository.py) |
| Run-owned schema projection | [`RunAwareSchemaRepository`](../../../src/impodo/adapters/duckdb/run_aware_schema_repository.py) |
| Run-owned reference projection | [`RunAwareAdvancedCoverageRepository`](../../../src/impodo/adapters/duckdb/run_aware_advanced_coverage_repository.py) |
| Browser routes | [`integrated_runs.py`](../../../src/impodo/web/routers/integrated_runs.py) |
| Workspace journey policy | [`workspace_journeys.py`](../../../src/impodo/web/workspace_journeys.py) |
| Journey-aware navigation | [`navigation.py`](../../../src/impodo/web/presenters/navigation.py) |

## Verification

- [`tests/test_integrated_recipe_runs.py`](../../../tests/test_integrated_recipe_runs.py)
- [`tests/test_workspace_journeys.py`](../../../tests/test_workspace_journeys.py)
- [`tests/test_project_authoring.py`](../../../tests/test_project_authoring.py)
- [`tests/test_data_version_source_packages.py`](../../../tests/test_data_version_source_packages.py)

## Related documentation

- [Data-manager guide](../../user/guides/integrated-test-runs.md)
- [Project lifecycle](../contracts/project-lifecycle.md)
- [Integrated run lifecycle contract](../contracts/integrated-run-lifecycle.md)
- [Evidence lifecycle](../contracts/evidence-lifecycle.md)
- [Qualification workflow](08-integrated-qualification.md)
