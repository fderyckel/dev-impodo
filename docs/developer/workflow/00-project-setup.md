---
audience: developer
stage: setup
status: current
---

# Data project and authoring workspace setup

## Responsibility

Setup creates a real `MigrationProject`, Authoring DataVersion 1, Authoring
MigrationRun 1, and one open MigrationWorkspace. It creates no Recipe. The
data manager can continue the normal workflow and complete one-off work without
ever publishing reusable rules.

## Entry conditions

The operator has an authenticated local session and can create Projects. The
new storage root must match the exact Project-root registry, Project-owned
DataVersion store, reference-only MigrationWorkspace store, and workspace-owned
engine generations. Supported older versions within those generations upgrade
transactionally before use. Retired generations require the reviewed
development reset.

## Implementation flow

`migration_projects.py` owns `/projects`, `/projects/new`, the Project
overview, and optional Recipe publication. `workspace_setup.py` opens the
contained authoring engine under `/workspaces/{workspace_id}` and directs file
or Odoo-source setup to the existing bounded services.

`WorkspaceAccessService` provides the verified Project-owned lineage for one
workspace through one registry read. `WorkspaceOwnerViewService` uses that
lineage to give presenters explicit Project, MigrationWorkspace, DataVersion,
MigrationRun, source-package, and run-target objects.
`WorkspaceAccessMiddleware` now resolves and binds that lineage before every
authenticated workspace route. Workspace application services reuse the same
context for exact capability checks, and background Odoo jobs receive it as an
immutable worker packet. Preparation, Odoo-capture, and load progress requests
reuse that verified packet, so a progress page does not reopen a worker-held
registry or add another Project lookup.

The New project form records Project name, migration purpose, source mode, and
source-system identity. `MigrationProjectAuthoringService` uses one browser
request ID and deterministic child operation IDs to create or resume each
root. It initializes a flat mapping-workbench projection only after the clean
MigrationWorkspace exists. That projection is not another identity or
lifecycle root.

## Evidence and state

The registry owns Project, DataVersion, run, workspace, Recipe, operation, and
run target-setup projections. `MigrationWorkspace` owns setup state and its
optimistic revision. The DataVersion store owns the canonical source package. The
workspace store owns its exact selected dataset references, while the
contained mapping engine keeps current authoring evidence.

## Source acceptance

`SourceIntakeService` receives a workspace ID, while
the explicit `DataVersionSourceArtifactStore` stores the source artifact under
`artifacts/dv/<data_version_id>`. The draft package records each file at
intake. `DataVersionOwnedSourceRepository` records inspection catalogues and
confirmed parsing choices in that package while maintaining bounded local
invalidation caches. When the data manager freezes tables,
`WorkspaceDataVersionSourceService` adds datasets and snapshot references,
freezes the DataVersion, and writes the workspace's bounded dataset projection.

Target setup follows the same single-owner rule. `MigrationRunTargetSetup`
owns the mutable Local or Remote Odoo choice before capture. All workspaces in
that run project the same setup. The existing immutable `TargetBinding` remains
the authority after capture.

`MappingWorkspaceService` and the Recipe compiler consume
`WorkspaceMappingSourceProjection`. They do not read a mutable workspace source
pointer as ownership and do not copy source files or rows.

For an Odoo source, the background capture publishes its immutable values and
protected origin sidecar under the same DataVersion artifact owner. The job is
successful only after `WorkspaceDataVersionSourceService` freezes the complete
Odoo package and materializes the workspace references.

## Optional publication

The Project overview asks `RecipePublicationService` for one readiness
projection. When eligible, the data manager can save a new Recipe or publish a
successor revision. `RecipeRepository` stores the protected payload and
registry revision through a recoverable operation. It leaves all Project and
DataVersion identities unchanged.

## Background preparation

The browser captures Project, DataVersion, run, and workspace identities before
spawning a preparation worker. It also captures the exact application build and
workspace schema contract. The worker proves that it loaded the same build
before it opens the fixed workspace engine. It then verifies `workspace.duckdb`
and the frozen `data-version.duckdb`, and routes source artifacts to the
DataVersion. It never opens the registry or loads a Recipe-root linkage.

## Completion and navigation

Creation completes when all four roots and the contained mapping engine exist.
The Project overview then opens the workspace under
`/workspaces/{workspace_id}`. Recipe publication is optional and does not gate
the six authoring stages.

## Invalidation and recovery

The creation request ID resumes the same bounded operation after interruption.
Reusing it for different Project meaning fails closed. Source acceptance is
immutable; replacement evidence requires a new DataVersion workflow rather
than editing the accepted package.

## Odoo 19 and performance

Project list and overview use bounded registry projections. Do not add a
workspace-store or protected-payload read inside a list loop. Connection checks
remain purpose-specific and do not perform model discovery. Mapping,
preparation, comparison, and Odoo access remain batched rather than per row.

## Data-manager concept help

The browser uses the common terms **data project**, **data version**,
**workspace**, **Recipe**, **Recipe version**, **Test run**, **Recipe work
area**, **Cutover plan**, and **Production run**. Internal class and persistence
names remain valid in developer contracts but do not appear in the normal
browser path.

`ConceptHelp` is immutable presentation data shared by `/concepts` and the
contextual dialog macro. The route requires the existing local session but does
not open the registry, a workspace store, a protected Recipe payload, or an
Odoo boundary. The data project list still performs one bounded summary query;
help content is never fetched once per project or once per icon.

Each contextual control is a normal deep link before JavaScript enhancement.
The generic listener opens the matching native dialog when supported and
returns focus to the link after close. The full page, dialog, and related links
therefore use one reviewed registry without adding a database or N+1 path.

## Code references

| Role | Code |
| --- | --- |
| Project-native routes | [`migration_projects.py`](../../../src/impodo/web/routers/migration_projects.py) |
| Workspace setup routes | [`workspace_setup.py`](../../../src/impodo/web/routers/workspace_setup.py) |
| Creation coordinator | [`MigrationProjectAuthoringService`](../../../src/impodo/application/migration_project_authoring_service.py) |
| Clean roots | [`MigrationProjectService`](../../../src/impodo/migration_projects.py), [`DataVersionService`](../../../src/impodo/data_versions.py), [`MigrationRunService`](../../../src/impodo/migration_runs.py), [`MigrationWorkspaceService`](../../../src/impodo/migration_workspaces.py) |
| Verified workspace lineage | [`WorkspaceAccessService`](../../../src/impodo/workspace_access.py) and [`MigrationFoundationRepository.resolve_workspace_access_context`](../../../src/impodo/adapters/duckdb/migration_foundation_repository.py) |
| Forward-only storage upgrades | [`ensure_current_schema`](../../../src/impodo/adapters/duckdb/schema/forward_upgrades.py) |
| Workspace authorization middleware | [`WorkspaceAccessMiddleware`](../../../src/impodo/web/security.py) |
| Workspace setup root | [`MigrationWorkspaceService`](../../../src/impodo/migration_workspaces.py) and `MigrationWorkspaceService.complete_setup` |
| Contained workbench | [`WorkspaceStateService`](../../../src/impodo/workspace_state.py) |
| Canonical workspace page | [`WorkspaceOwnerViewService`](../../../src/impodo/workspace_views.py) |
| Source ownership cutover | [`DataVersionOwnedSourceRepository`](../../../src/impodo/adapters/duckdb/data_version_source_repository.py), [`WorkspaceDataVersionSourceService`](../../../src/impodo/application/workspace_data_version_source_service.py) |
| Owner-specific artifact stores | [`DataVersionSourceArtifactStore` and `WorkspaceArtifactStore`](../../../src/impodo/artifacts.py) |
| Run target setup | [`MigrationRunTargetSetupService`](../../../src/impodo/migration_run_setup.py) |
| Optional compilation and publication | [`RecipeCompiler.compile_workspace`](../../../src/impodo/application/recipe_compilation_service.py), [`RecipePublicationService`](../../../src/impodo/application/recipe_publication_service.py) |
| Odoo connection boundary | [`OdooConnectionTestService`](../../../src/impodo/application/odoo_connection_service.py) |
| Navigation | [`build_workspace_navigation`](../../../src/impodo/web/presenters/navigation.py) |
| Data-manager concept registry | [`ConceptHelp`](../../../src/impodo/web/presenters/concepts.py) |
| Read-only Concepts route | [`concepts.py`](../../../src/impodo/web/routers/concepts.py) |
| Browser composition | [`app.py`](../../../src/impodo/web/app.py) |

## Verification

- [`tests/test_project_authoring.py`](../../../tests/test_project_authoring.py)
- [`tests/test_identity_semantics.py`](../../../tests/test_identity_semantics.py)
- [`tests/test_workspace_access.py`](../../../tests/test_workspace_access.py)
- [`tests/test_canonical_ownership.py`](../../../tests/test_canonical_ownership.py)
- [`tests/test_workspace_evidence_storage.py`](../../../tests/test_workspace_evidence_storage.py)
- [`tests/test_forward_upgrade_compatibility.py`](../../../tests/test_forward_upgrade_compatibility.py)
- [`tests/test_concept_help.py`](../../../tests/test_concept_help.py)
- [`tests/test_project_security.py`](../../../tests/test_project_security.py)
- [`tests/test_odoo_connection_service.py`](../../../tests/test_odoo_connection_service.py)
- [`tests/test_odoo_capture_jobs.py`](../../../tests/test_odoo_capture_jobs.py)
- [`tests/test_odoo_capture_publication.py`](../../../tests/test_odoo_capture_publication.py)
- [`tests/test_preparation_jobs.py`](../../../tests/test_preparation_jobs.py)
- [`tests/test_recipe_representative_shapes.py`](../../../tests/test_recipe_representative_shapes.py)

## Related documentation

- [User setup guide](../../user/getting-started.md)
- [Project lifecycle contract](../contracts/project-lifecycle.md)
- [Integrated multi-Recipe Test runs](07-integrated-test-runs.md)
- [Recipe publication contract](../contracts/recipe-lifecycle.md)
- [Source data implementation](01-source-data.md)
