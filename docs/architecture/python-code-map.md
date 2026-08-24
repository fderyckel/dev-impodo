---
audience: developer
kind: reference
status: current
---

# Python code map

## Read this first

Impodo separates the Project business root from the technical workspace and
from optional reusable Recipes. Begin with the actor action you are tracing,
then follow the service to its exact repository.

## Project-first composition

| Responsibility | Domain or application boundary | Persistence or browser boundary |
| --- | --- | --- |
| Explain data-manager concepts | immutable `ConceptHelp` presentation registry | authenticated read-only `/concepts` route and shared dialog macro |
| List and read Projects | `migration_projects.py` | `MigrationFoundationRepository`, `/projects` |
| Create a Project and first Authoring context | `MigrationProjectAuthoringService` | `migration_projects.py` router |
| Own a complete source package | `DataVersion`, `DataVersionSourcePackage` | `data-version.duckdb` and `artifacts/dv/<data_version_id>` |
| Coordinate one target use | `MigrationRun`, `MigrationRunTargetSetup` | registry run and target-setup projections |
| Isolate current working evidence | `MigrationWorkspace` plus current workspace services | `workspace.duckdb`, contained `workspace-engine.duckdb`, `artifacts/ws/<workspace_id>`, `/workspaces/{workspace_id}` |
| Authorize a workspace against its Project | `WorkspaceAccessService`, `WorkspaceAuthorizationPolicy` | `WorkspaceAccessMiddleware`, immutable worker access context reused by progress routes |
| Render canonical workspace owners | `WorkspaceOwnerViewService` | explicit Project, workspace, DataVersion, run, package, and target view |
| Supply mapping source contracts | `WorkspaceMappingSourceProjection` | bounded workspace source projection |
| Compile reusable meaning | `RecipeCompiler.compile_workspace` | reads current workspace evidence only |
| Publish optional Recipes | `RecipePublicationService` | `RecipeRepository`, protected Recipe store |
| Plan an integrated Test run | `MigrationRunPlanningService` | `MigrationRunPlanningRepository`, Project run routes |
| Materialize a fresh Recipe application | `RecipeApplicationService` | one isolated workspace and run-aware target projections |
| Version and qualify an integrated plan | `CutoverPlanService` | `CutoverPlanRepository`, protected Project evidence, qualification routes |
| Run selected meaning with latest data | `ProductionCutoverService` | `ProductionRunRepository`, Production run routes, shared workspace engine |

`web/app.py` composes these Project-first boundaries, including the current
Production coordinator. It does not compose the superseded Recipe-root list,
creation, deletion, Test-application, Production-application, or qualification
services.

## Creation trace

1. `web/routers/migration_projects.py` validates the `/projects/new` form.
2. `MigrationProjectAuthoringService.create` creates a `MigrationProject`.
3. It creates Authoring `DataVersion` 1 and an empty draft source package.
4. It creates Authoring `MigrationRun` 1 and one open `MigrationWorkspace`.
5. `WorkspaceStateService.provision_migration_workspace` initializes the
   mapping workbench under that exact workspace ID. The registry remains the
   only workspace identity and lifecycle owner.
6. Deterministic child operation IDs let the coordinator resume after a fault
   without creating duplicate roots.

The four identities are distinct and no Recipe row is created.

The global `/concepts` page is intentionally outside this creation trace. It
renders the static `ConceptHelp` registry and does not open the Project
registry, workspace evidence, Recipe payloads, or Odoo. Contextual help uses
the same registry, so adding help beside a Project row must not add a per-row
query.

## Source acceptance trace

1. `SourceIntakeService` receives source bytes for a workspace.
2. `LocalArtifactStore` implements the explicit
   `DataVersionSourceArtifactStore` port and stores source bytes under
   `artifacts/dv/<data_version_id>`.
3. `MigrationWorkspaceStateRepository` records the file immediately in the
   draft `DataVersionSourcePackage`; the local file row is a derived workbench
   cache.
4. `DataVersionOwnedSourceRepository` advances canonical inspection catalogues
   and parsing confirmations while keeping local invalidation tables aligned.
5. `WorkspaceDataVersionSourceService` adds logical datasets and immutable
   snapshot references to that same package.
6. `DataVersionSourcePackageService.freeze` freezes both the package and
   DataVersion identity.
7. `WorkspaceSourceProjectionService` writes only selected dataset IDs and
   snapshot hashes to `workspace.duckdb`.
8. `WorkspaceMappingSourceProjection` supplies those immutable contracts to
   the mapping editor and Recipe compiler.

No source row or source artifact is copied into the clean workspace store.

## Recipe publication trace

1. The Project overview asks `RecipePublicationService.draft` whether
   the authoring workspace is eligible.
2. `RecipeCompiler` compiles portable semantic meaning and validates
   the exact envelope contract in `domain/recipe_envelope.py`.
3. `RecipeRepository` reserves an owner-specific operation intent.
4. It writes the authenticated payload to the protected Recipe store.
5. One registry transaction creates the Recipe identity and revision 1, or
   appends a successor revision with optimistic concurrency.
6. Replay resumes from the stored intent and returns the one committed result.

Publication records origin provenance. It never updates DataVersion ownership,
Project identity, workspace identity, run identity, or cutover authority.

## Integrated Test run trace

1. The Project route resolves one accepted Test DataVersion, exact Recipe
   revisions, explicit dependencies, and one reviewed Authoring target.
2. `MigrationRunPlanningService.review_test_run` validates each revision once,
   rejects cycles and overlapping writable fields, and creates a canonical
   union requirement plan.
3. One run-owned schema projection and supporting-reference bundle are
   filtered to that union. `MigrationRunTargetSchema` and
   `MigrationRunReferenceBundle` use `migration_run_id`, retain their source
   workspace provenance, and never place a run UUID in a workspace field.
   There is no Odoo capture per Recipe.
4. `MigrationRunPlanningRepository.provision_integrated_run` creates the run,
   target binding, applications, and distinct workspaces in one restart-safe
   operation.
5. `RecipeApplicationService` selects each application's DataVersion
   datasets and builds fresh mapping evidence through the existing mapping
   service.
6. The run page reads status and issues through bounded registry queries and
   does not open every workspace.
7. `CutoverPlanRepository.ensure_for_run` reuses unchanged plan meaning or
   appends a new unqualified revision and binds the run exactly.

## Integrated qualification trace

1. The qualification route resolves the run's exact CutoverPlan revision.
2. `WorkspaceIntegratedQualificationEvidenceReader` reads current mapping,
   preparation, quality, comparison, execution, and reconciliation evidence
   from each application without contacting Odoo.
3. `CutoverPlanService` checks package completeness, full application
   membership, shared controls, and dependency time order.
4. `CutoverPlanRepository` encrypts application and integrated evidence, then
   publishes all qualification rows in one restart-safe registry transaction.
5. A separate operation selects only a qualification for the current plan
   revision. It creates no Production authority.
6. Before any downstream write probe, `execution.py` asks the service to prove
   current verified reconciliation for every predecessor.

## Production rollout trace

1. `ProductionCutoverService.start_setup` authenticates the current selected
   qualification and creates a fresh Production DataVersion, run, and setup
   workspace without target or write authority.
2. The existing workspace source and schema services accept the complete
   latest package and capture the different Production Odoo 19 target with
   read-only credential evidence.
3. `MigrationRunPlanningService.review_production_run` recompiles current
   bindings, parameters, controls, requirements, references, dependencies, and
   write ownership against the exact selected plan.
4. `MigrationRunPlanningRepository.activate_production_run` records one
   run-level target and requirement capture plus isolated applications in a
   restart-safe transaction.
5. The shared application materializer creates fresh workspace engines and
   mapping drafts using immutable DataVersion references. It copies no Test
   workspace evidence.
6. `ProductionCutoverService.assert_execution_authority` rechecks selection,
   target, read/write identities, and credential generations before
   `execution.py` constructs a writer.

## Existing workspace workflow

| Stage | Main application service | Browser route prefix |
| --- | --- | --- |
| Setup | `WorkspaceStateService`, `SourceIntakeService` | `/workspaces/{workspace_id}` |
| Source data | `SourceWorkspaceService`, `OdooCapturePublicationService`, `OdooCaptureJobManager` | `/workspaces/{workspace_id}/sources` |
| Odoo data | `SchemaWorkspaceService` | `/workspaces/{workspace_id}/schema` |
| Match data | `MappingWorkspaceService` | `/workspaces/{workspace_id}/mapping` |
| Prepare data | `PreparationService`, `PreparationJobManager` | `/workspaces/{workspace_id}/prepare` |
| Final review | `PreflightService` | `/workspaces/{workspace_id}/summary` |
| Load and reconcile | `ExecutionService`, `LoadJobManager`, `ReconciliationService` | `/workspaces/{workspace_id}/load` |

## Query and Odoo performance

`MigrationFoundationRepository.list_projects`, run history, integrated
progress, and application issues use bounded registry projections. The
Project overview's single Recipe-publication readiness check may open its one
Authoring workspace; it must not grow into one workspace open per list row.
Mapping, preparation, and comparison may stream or batch rows, but they must
not issue one repository or Odoo request per row, field, or relationship.

Odoo adapters expose closed Odoo 19 operations. Search and schema reads are
batched by model, target keys are indexed once, and write authority remains
separate from read capability.

## Focused verification

- `tests/test_migration_project_contracts.py`
- `tests/test_migration_foundation.py`
- `tests/test_data_version_source_packages.py`
- `tests/test_project_authoring.py`
- `tests/test_integrated_recipe_runs.py`
- `tests/test_cutover_qualification.py`
- `tests/test_production_rollout.py`
- `tests/test_recipe_representative_shapes.py`
- `tests/test_preparation_jobs.py`
