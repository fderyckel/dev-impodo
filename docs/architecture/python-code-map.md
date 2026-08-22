---
audience: developer
kind: reference
status: current
---

# Python code map

## Read this first

Impodo separates the Project business root from the technical workspace and
from optional reusable Recipes. Begin with the actor action you are tracing,
then follow the service to its exact repository. Do not infer ownership from
the historical `project.duckdb` filename or `WorkspaceState` type name.

## Project-first composition

| Responsibility | Domain or application boundary | Persistence or browser boundary |
| --- | --- | --- |
| List and read Projects | `migration_projects.py` | `MigrationFoundationRepository`, `/projects` |
| Create a Project and first Authoring context | `MigrationProjectAuthoringService` | `migration_projects.py` router |
| Own a complete source package | `DataVersion`, `DataVersionSourcePackage` | `data-version.duckdb` and DataVersion artifact directory |
| Coordinate one target use | `MigrationRun` | registry `migration_run` projection |
| Isolate current working evidence | `MigrationWorkspace` plus current workspace services | `workspace.duckdb`, contained `project.duckdb`, `/workspaces/{workspace_id}` |
| Supply mapping source contracts | `WorkspaceMappingSourceProjection` | bounded workspace source projection |
| Compile reusable meaning | `RecipeAuthoringService.compile_workspace` | reads current workspace evidence only |
| Publish optional Recipes | `ProjectRecipePublicationService` | `ProjectRecipeRepository`, protected Recipe store |

`web/app.py` composes these boundaries. It does not compose the superseded
Recipe-root list, creation, deletion, Test-application, Production-application,
or qualification services.

## Creation trace

1. `web/routers/migration_projects.py` validates the `/projects/new` form.
2. `MigrationProjectAuthoringService.create` creates a `MigrationProject`.
3. It creates Authoring `DataVersion` 1 and an empty draft source package.
4. It creates Authoring `MigrationRun` 1 and one open `MigrationWorkspace`.
5. `ProjectService.provision_migration_workspace` initializes the existing
   mapping engine under that exact workspace ID.
6. Deterministic child operation IDs let the coordinator resume after a fault
   without creating duplicate roots.

The four identities are distinct and no Recipe row is created.

## Source acceptance trace

1. `SourceIntakeService` receives source bytes for a workspace.
2. `DataVersionAwareArtifactStore` resolves the workspace's DataVersion and
   stores source bytes under that DataVersion ID.
3. Existing inspection and source services record current authoring choices in
   the contained mapping engine.
4. `WorkspaceDataVersionSourceService` converts the accepted file, catalogue,
   confirmation, dataset, and snapshot contracts into one canonical
   `DataVersionSourcePackage`.
5. `DataVersionSourcePackageService.freeze` freezes both the package and
   DataVersion identity.
6. `WorkspaceSourceProjectionService` writes only selected dataset IDs and
   snapshot hashes to `workspace.duckdb`.
7. `WorkspaceMappingSourceProjection` supplies those immutable contracts to
   the mapping editor and Recipe compiler.

No source row or source artifact is copied into the clean workspace store.

## Recipe publication trace

1. The Project overview asks `ProjectRecipePublicationService.draft` whether
   the authoring workspace is eligible.
2. `RecipeAuthoringService` compiles portable semantic meaning and validates
   the exact envelope contract in `domain/recipe_envelope.py`.
3. `ProjectRecipeRepository` reserves an owner-specific operation intent.
4. It writes the authenticated payload to the protected Recipe store.
5. One registry transaction creates the Recipe identity and revision 1, or
   appends a successor revision with optimistic concurrency.
6. Replay resumes from the stored intent and returns the one committed result.

Publication records origin provenance. It never updates DataVersion ownership,
Project identity, workspace identity, run identity, or cutover authority.

## Existing workspace workflow

| Stage | Main application service | Browser route prefix |
| --- | --- | --- |
| Setup | `ProjectService`, `SourceIntakeService` | `/workspaces/{workspace_id}` |
| Source data | `SourceWorkspaceService`, `OdooCapturePublicationService` | `/workspaces/{workspace_id}/sources` |
| Odoo data | `SchemaWorkspaceService` | `/workspaces/{workspace_id}/schema` |
| Match data | `MappingWorkspaceService` | `/workspaces/{workspace_id}/mapping` |
| Prepare data | `PreparationService`, `PreparationJobManager` | `/workspaces/{workspace_id}/prepare` |
| Final review | `PreflightService` | `/workspaces/{workspace_id}/summary` |
| Load and reconcile | `ExecutionService`, `ReconciliationService` | `/workspaces/{workspace_id}/load` |

The route parameter is still named `project_id` in some contained engine
functions. Its value is a MigrationWorkspace ID. New code should use
`workspace_id`; it must not recreate a Project-as-workspace alias.

## Query and Odoo performance

`MigrationFoundationRepository.list_projects` and Project overview lists read
bounded registry projections. Do not open each DataVersion or workspace store
inside a list loop. Mapping, preparation, and comparison may stream or batch
rows, but they must not issue one repository or Odoo request per row, field, or
relationship.

Odoo adapters expose closed Odoo 19 operations. Search and schema reads are
batched by model, target keys are indexed once, and write authority remains
separate from read capability.

## Focused verification

- `tests/test_migration_project_phase_m0_contract.py`
- `tests/test_migration_project_phase_m1_foundation.py`
- `tests/test_migration_project_phase_m2_source_packages.py`
- `tests/test_migration_project_phase_m3_project_authoring.py`
- `tests/test_recipe_representative_shapes.py`
- `tests/test_preparation_jobs.py`
