---
audience: developer
kind: reference
status: current
---

# Python code map

## Read this first

Impodo separates the Project business root from the technical workspace and
from optional reusable Recipes. Begin with the actor action you are tracing,
then follow the service to its exact repository. Use the
[code-organization guide](code-organization.md) when deciding where new code
belongs or which dependencies it may introduce.

## Project-first composition

| Responsibility | Domain or application boundary | Persistence or browser boundary |
| --- | --- | --- |
| Explain data-manager concepts | immutable `ConceptHelp` presentation registry | authenticated read-only `/concepts` route and shared dialog macro |
| List and read Projects | `domain/project/models.py`, `application/project/service.py` | `MigrationFoundationRepository`, `/projects` |
| Create a Project and first Authoring context | `MigrationProjectAuthoringService` | `web/routers/migration_projects.py` |
| Own a complete source package | `DataVersion`, `DataVersionSourcePackage` | `data-version.duckdb` and `artifacts/dv/<data_version_id>` |
| Coordinate one target use | `domain/run/models.py`, `application/run/service.py`, `MigrationRunTargetSetup` | registry run and target-setup projections |
| Isolate current working evidence | `MigrationWorkspace` plus current workspace services | `workspace.duckdb`, contained `workspace-engine.duckdb`, `artifacts/ws/<workspace_id>`, `/workspaces/{workspace_id}` |
| Authorize a workspace against its Project | `WorkspaceAccessService`, `WorkspaceAuthorizationPolicy` | `WorkspaceAccessMiddleware`, immutable worker access context reused by progress routes |
| Render canonical workspace owners | `WorkspaceOwnerViewService` | explicit Project, workspace, DataVersion, run, package, and target view |
| Supply mapping source contracts | `WorkspaceMappingSourceProjection` | bounded workspace source projection |
| Compile reusable meaning | `RecipeCompiler.compile_workspace` | reads current workspace evidence only |
| Publish optional Recipes | `RecipePublicationService` | `RecipeRepository`, protected Recipe store |
| Plan an integrated Test run | focused use cases under `application/run`; `MigrationRunPlanningService` is the stable facade | `MigrationRunPlanningRepository`, Project run routes |
| Materialize a fresh Recipe application | `RecipeApplicationService` | one isolated workspace and run-aware target projections |
| Coordinate Review and load progress | `web/run_review.py` | bounded registry status plus latest preparation and load job snapshots |
| Recover an interrupted Odoo batch | `application/workspace/execution/reconciliation.py` assesses exact read-back; `ExecutionService.resume` classifies the same frozen schedule | `ExecutionRepository.record_batch_started` and `record_recovery` persist the checkpoint and report binding inside the existing row journal |
| Seal, review, execute, and verify completed-load correction intent | `domain/correction.py`, `domain/correction_origin.py`, `domain/correction_execution.py`, `application/correction_service.py`, `application/correction_orchestration.py`, `application/correction_stages.py`, `application/correction_workflow.py`, and `application/correction_execution.py` | `web/routers/corrections.py` and `CorrectionJobManager` present one focused resumable journey; native Polars/Parquet review emits sparse intent; exact protected IDs drive bounded reread, writes, and existing execution/reconciliation journals; one registry binding and whole-artifact hashes avoid row hashes |
| Version and qualify an integrated plan | `domain/cutover/models.py`, `CutoverPlanService` | `CutoverPlanRepository`, protected Project evidence, qualification routes |
| Run selected meaning with latest data | `ProductionCutoverService` | `ProductionRunRepository`, Production run routes, shared workspace engine |

`web/app.py` composes these Project-first boundaries, including the current
Production coordinator. It does not compose the superseded Recipe-root list,
creation, deletion, Test-application, Production-application, or qualification
services.

## Layer and capability index

| Code to find | Current path |
| --- | --- |
| Project, Data version, workspace, Recipe, run, and Cutover meaning | `domain/project`, `domain/data_version`, `domain/workspace`, `domain/recipe`, `domain/run`, and `domain/cutover` |
| Portable Mapping, Preparation, and Execution decisions | `domain/mapping`, `domain/compiler`, `domain/relationship_dependencies.py`, `domain/preparation`, `domain/staging`, `domain/execution_snapshot.py`, and `domain/execution` |
| Owner-qualified commands, queries, and ports | `application/project`, `application/data_version`, `application/recipe`, `application/run`, and `application/workspace` |
| Cross-owner workflow coordinators and stable facades | Named modules directly below `application`, including Project authoring, Recipe compilation and publication, source projection, preflight, Cutover, and Production coordination. |
| Shared application ports | `application/shared` |
| DuckDB stores and forward-only schema handling | `adapters/duckdb` and `adapters/duckdb/schema` |
| Artifact, protected-evidence, job, Odoo, and columnar implementations | `adapters/artifacts`, `adapters/protected_evidence`, `adapters/jobs`, and `adapters/odoo`, plus named integration facades directly below `adapters` |
| Request handling and view construction | `web/routers` and `web/presenters` |
| Concrete runtime construction | `web/composition`, `web/app.py`, and `web/capability_builders.py` |
| Server-rendered pages and page-owned browser behavior | `web/templates`, `web/templates/mapping`, and `web/static` |
| Focused evidence | `tests/architecture`, `tests/domain`, `tests/application`, `tests/integration`, `tests/e2e`, and `tests/performance` |
| Explicit non-discovered test builders and paths | `tests/support` |

## Maintenance boundaries

Use these paths when extending an existing capability. The facade modules keep
established ports and composition stable; new decisions belong in the focused
owner module.

| Capability | Focused owner modules | Stable facade or transaction boundary |
| --- | --- | --- |
| Test setup | `application/run/test_setup_start.py`, `fresh_data_setup.py`, and `odoo_requirements.py` | `application/run/test_setup_service.py` |
| Integrated run review and activation | `application/run/review.py`, `test_activation.py`, `production_review.py`, `production_activation.py`, `application_materialization.py`, and `application_recovery.py` | `application/run/planning_service.py` |
| Shared registry | `foundation_project_records.py`, `foundation_data_version_records.py`, `foundation_data_version_commands.py`, `foundation_migration_run_records.py`, `foundation_migration_run_commands.py`, `foundation_workspace_records.py`, and `foundation_workspace_commands.py` | `migration_foundation_repository.py` assembles the adapter; `registry_transaction.py` alone owns the shared DuckDB commit or rollback boundary |
| Registry operation and serialization support | `foundation_operation_intents.py`, `foundation_source_package_records.py`, `foundation_registry_support.py`, and `foundation_record_codecs.py` | private adapter support only; application services never receive a connection |
| Preparation publication | `preparation_direct_writer.py`, `preparation_quality_index.py`, `preparation_normalization_records.py`, `preparation_stored_run_reader.py`, and `preparation_failure_cleanup.py` | `preparation_session_repository.py` keeps the public preparation port and connection policy |
| Preparation bindings | `preparation_snapshot_bindings.py`, `preparation_derived_artifact_bindings.py`, `preparation_canonical_projection_bindings.py`, and `preparation_session_lifecycle.py` | each collaborator reuses the transaction opened for its one publication or lifecycle action |
| Mapping state and decisions | `domain/workspace/contracts.py`, `domain/mapping`, and `application/workspace/mapping` | a workspace owns mutable evidence; Recipe publication receives only portable rules |
| Preparation state and decisions | `domain/preparation` and `application/workspace/preparation` | `web/composition/preparation_job_manager.py` and `preparation_worker.py` own the local process runtime |
| Final review evidence | `domain/preflight` defines portable report and prepared-cell meaning; `application/preflight_service.py` binds current workspace evidence | `adapters/artifacts/reporting.py` renders the manifest-authoritative workbook; browser routes only request and serve the artifact |
| Matching review evidence | `domain/mapping` defines the checked revision, validation issues, coverage, and deferred checks | `adapters/artifacts/mapping_review.py` renders the Stage 3 workbook without preparing rows or contacting Odoo; `web/routers/mapping.py` creates and serves the exact-revision artifact |
| Execution state and decisions | `domain/run`, `domain/execution`, `application/run`, and `application/workspace/execution`; `ExecutionPreview` and `LoadJob` expose bounded read-only browser projections | `adapters/odoo` implements the target ports; `web/composition/target_readers.py` and `target_writers.py` select implementations, while `web/routers/execution.py` presents but never recalculates the frozen order |
| Completed-load correction decisions | `domain/correction.py`, `domain/correction_origin.py`, `domain/correction_execution.py`, `application/correction_service.py`, `application/correction_orchestration.py`, `application/correction_stages.py`, `application/correction_workflow.py`, and `application/correction_execution.py` | `adapters/polars_correction.py` emits sparse typed A/C differences from immutable prepared Parquet artifacts; `CorrectionReviewService` resolves distinct exact-existing many-to-one keys in bounded groups; `adapters/correction_review_pipeline.py` requires native programs; `web/routers/corrections.py` exposes safe counts and resumable progress only; the correction executor uses exact protected IDs and the existing scoped JSON-2, execution-journal, and reconciliation adapters without related-record writes; `adapters/duckdb/correction_repository.py` owns current pointers and verified successor completion |
| Artifact and secret storage | `application/shared/artifacts.py` and `application/shared/secrets.py` | concrete filesystem and credential implementations live below `adapters/artifacts`, `adapters/protected_evidence`, and `adapters/protected_project_evidence_store.py`; typed Project artifacts reuse that encrypted store through focused adapter facades |

Do not add a DuckDB connection parameter to an application use case. Add a
named atomic command to the consumer-owned port, implement it behind the
adapter facade, and preserve its fault-replay and query-count test. When a
preparation test must patch an implementation detail, patch the focused module
that owns it, not `preparation_session_repository.py`.

Do not add a new module directly below `src/impodo`. `__main__.py` is the only
root entry point. Put portable meaning below `domain`, coordination and its
consumer-owned ports below `application`, implementations below `adapters`,
and runtime construction below `web/composition`. The architecture test fails
on a flat or otherwise unclassified production path.

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

## Forward storage upgrade trace

1. A store opener reads only the `schema_version` identity.
2. `schema/forward_upgrades.py` rejects another generation, a version outside
   the supported range, or an incomplete version path before any write.
3. The store-specific registry applies every consecutive structural step in
   one DuckDB transaction and records it in `schema_migration`.
4. The store's current validator checks the complete table and column shape
   before the transaction commits.
5. Normal repositories then use only the current schema. They contain no old
   field branch, row conversion loop, Odoo call, downgrade, or dual write.

The Project registry opens first. DataVersion, MigrationWorkspace reference,
and workspace-engine databases upgrade when their authorized owner opens them.
An interruption between databases is resumable because each database is
independently either unchanged or fully current.

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

1. The Project route creates one Test setup over exact Recipe revisions and
   explicit dependencies, then accepts one fresh Test DataVersion.
2. `TestRunOdooRequirementsUseCase.for_workspace` authorizes the run-owned
   query, bulk-reads the selected revisions, and unions their Odoo models,
   fields, and Recipe-owned relationship paths without contacting Odoo per
   Recipe. `TestRunSetupService` retains the stable query used by browser
   contexts and delegates the decision.
3. The run-owned **Check Odoo** route presents that scope as read-only and
   delegates field capture to the existing shared schema service. A setup
   workspace schema URL redirects to the run; Authoring keeps the editable
   schema route. `_capture_recipe_supporting_values` unions related model fields
   and performs one bounded supporting-value reader call for the run.
4. The run-owned Odoo-check `POST` calls
   `MigrationRunPlanningService.activate_test_run` after schema and supporting
   values are current. The planner validates each revision once,
   rejects cycles and overlapping writable fields, and creates a canonical
   union requirement plan.
5. One run-owned schema projection and supporting-reference bundle are
   filtered to that union. `MigrationRunTargetSchema` and
   `MigrationRunReferenceBundle` use `migration_run_id`, retain their source
   workspace provenance, and never place a run UUID in a workspace field.
   There is no Odoo capture per Recipe.
6. `MigrationRunPlanningRepository.provision_integrated_run` creates the run,
   target binding, applications, and distinct workspaces in one restart-safe
   operation.
7. `RecipeApplicationService` selects each application's DataVersion datasets,
   builds fresh mapping evidence, and checks and submits a clean mapping through
   the existing mapping service. A new warning or invalid result blocks.
8. `run_review.py` starts only the first safe Test preparation and builds
   ordered cards from bounded registry reads plus one latest-snapshot pass per
   job manager. The run page and status poll do not open every workspace.
9. Preparation and load workers publish coarse run milestones. Detailed
   prepared rows, comparison, execution journal, and reconciliation remain in
   each isolated workspace; only verified reconciliation unlocks the next
   Recipe.
10. `CutoverPlanRepository.ensure_for_run` reuses unchanged plan meaning or
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
progress, and application issues use bounded registry projections. Review and
load obtains latest preparation and load snapshots for all Recipe workspaces
in one in-memory pass per manager. The
Project overview's single Recipe-publication readiness check may open its one
Authoring workspace; it must not grow into one workspace open per list row.
Mapping, preparation, and comparison may stream or batch rows, but they must
not issue one repository or Odoo request per row, field, or relationship.

Odoo adapters expose closed Odoo 19 operations. Search and schema reads are
batched by model, target keys are indexed once, and write authority remains
separate from read capability.

## Focused verification

- `tests/architecture/test_inventory.py`
- `tests/architecture/test_dependency_rules.py`
- `tests/architecture/test_test_organization.py`
- `tests/architecture/test_static_asset_ownership.py`
- `tests/domain/project/test_contracts.py`
- `tests/integration/duckdb/test_migration_foundation.py`
- `tests/integration/duckdb/test_forward_upgrades.py`
- `tests/application/data_version/test_source_packages.py`
- `tests/application/project/test_authoring.py`
- `tests/application/run/test_odoo_requirements.py`
- `tests/application/run/test_integrated_recipe_runs.py`
- `tests/application/cutover/test_qualification.py`
- `tests/application/run/test_production_rollout.py`
- `tests/domain/recipe/test_representative_shapes.py`
- `tests/application/workspace/preparation/test_jobs.py`

The [code-organization regression
baseline](../testing/code-organization-phase0-baseline.md) lists the exact
fault-retry, bounded-I/O, fixed-seed, browser, and complete-discovery commands.
