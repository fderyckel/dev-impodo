---
audience: developer
kind: reference
status: current
---

# Python code map

Use this map to trace a browser action from its route to the application
service and the code that stores evidence or calls Odoo. Python paths are
relative to [`src/impodo`](../../src/impodo). The
[code organization guide](code-organization.md) explains where new code
belongs.

## Start at the browser action

| Action | Browser entry | Application owner |
| --- | --- | --- |
| Create or open a Project | `web/routers/migration_projects.py` | `MigrationProjectAuthoringService` creates the Authoring context. `application/project/service.py` reads Projects. |
| Select and accept source data | `web/routers/sources.py` and `derived_entities.py` | `SourceWorkspaceService` handles source choices. `OdooSourceCaptureService` assesses an Odoo source, and `OdooCapturePublicationService` publishes the capture. `DataVersionSourcePackageService` accepts the source package. |
| Capture the target schema and confirm matching rules | `web/routers/target.py` and `schema.py` | `SchemaWorkspaceService` governs the captured schema. `domain/workspace/business_keys.py` supplies matching-rule defaults. |
| Match data | `web/routers/mapping.py` and the focused review routers | `MappingWorkspaceService` saves and validates mapping decisions. `MatchingOrderService` derives advice and runs the explicit read-only matching check. |
| Prepare data and review quality | `web/routers/preparation.py` and `quality.py` | `PreparationService` transforms the selected source data. `QualityService` applies quality rules to the prepared result. |
| Compare with Odoo and review changes | `web/routers/summary.py` and `preflight.py` | `PreflightService` binds current preparation, target reads, and decisions to a comparison. |
| Load and verify changes | `web/routers/execution.py` | `ExecutionService` runs the confirmed schedule. `ReconciliationService` verifies the result. |
| Load an Odoo-to-Odoo transfer | `web/routers/transfer_load.py` | `TransferExecutionService` compiles the transfer from reviewed source and destination evidence. `ExecutionService` loads it and handles recovery. |
| Save and restore clean Odoo data | `web/routers/saved_odoo_data.py` | `SavedOdooDataService` owns capture configurations and retained data versions. `web/composition/saved_odoo_restore.py` checks a fresh destination and applies field policies through the transfer engine. `OdooSavedDataArchiveService` governs portable download and import. |
| Continue or inspect a completed load | `web/routers/history.py`, `continuation.py`, and `corrections.py` | `application/continuation_publication.py` retains the next cycle's full target index. `application/correction_workflow.py` coordinates the review. `application/correction_execution.py` uses exact protected record IDs and the scoped Odoo writer and reconciliation services. |
| Save a Recipe | `web/routers/migration_projects.py` | `RecipePublicationService` checks eligibility and publishes the revision. `RecipeCompiler` compiles portable rules from the selected source contracts and current workspace evidence. |
| Set up and run an integrated Test | `web/routers/integrated_runs.py` and `run_fresh_data.py` | `MigrationRunPlanningService` plans and activates the run. `RecipeApplicationService` builds fresh mapping evidence for each application. |
| Qualify an integrated Test | `web/routers/cutover_plans.py` | `CutoverPlanService` checks the exact plan revision and each application's evidence. |
| Run Production with current data | `web/routers/production_runs.py` and `run_fresh_data.py` | `ProductionCutoverService` starts setup, resumes activation, and checks execution authority. `MigrationRunPlanningService` reviews and activates the run. |

The [developer workflow pages](../developer/workflow/) give the routes, entry
conditions, evidence, and tests for each stage. The authenticated `/concepts`
route renders the static `ConceptHelp` registry. Contextual help uses that same
registry.

## Project, source data, and Recipe ownership

`MigrationProjectAuthoringService.create` creates the Project, its draft
Authoring Data version, an Authoring run, and a workspace.
`WorkspaceStateService.provision_migration_workspace` initializes the
workbench under that workspace ID. The
[`MigrationFoundationRepository`](../../src/impodo/adapters/duckdb/migration_foundation_repository.py)
stores the registry records. Deterministic child operation IDs let creation
resume after an interruption.

The Data version owns the source files, parsing choices, datasets, and frozen
snapshots. `LocalArtifactStore` writes source artifacts under
`artifacts/dv/<data_version_id>`, and
`DataVersionOwnedSourceRepository` keeps the source records in the Data
version store. When the source is accepted, `WorkspaceSourceProjectionService`
stores the selected dataset IDs and snapshot hashes in `workspace.duckdb`.
`WorkspaceMappingSourceProjection` gives the mapping editor and Recipe compiler
those source contracts. The contained `workspace-engine.duckdb` stores current
working evidence.

`WorkspaceAccessService` and `WorkspaceAccessMiddleware` resolve and authorize
the workspace against its Project before opening child evidence.
`WorkspaceOwnerViewService` reads the owners and target binding through one
registry connection. The source details stay in the Data version store.

Recipe publication saves an authenticated payload through
`RecipeRepository`. The registry transaction creates the Recipe and its first
revision or appends a successor with an optimistic revision check. A stored
operation intent lets a retry return the same result. See the
[Project lifecycle](../developer/contracts/project-lifecycle.md) and
[Recipe lifecycle](../developer/contracts/recipe-lifecycle.md) contracts.

## Saved Odoo data and repeat restoration

`domain/odoo_saved_data.py` separates capture definitions, immutable data-version
references, destination profiles, restore generations and capture operations.
`application/odoo_saved_data_ports.py` declares registry persistence.
`adapters/duckdb/odoo_saved_data_repository.py` stores those identities without
copying source rows into the registry.

`application/odoo_saved_data_service.py` checks source context, compiles capture
choices and publishes complete saved versions. Its progress owner is
`application/odoo_saved_data_jobs.py`. The source access boundary lives in
`web/composition/saved_odoo_data.py`.

`application/odoo_saved_data_workspaces.py` creates successor captures and
independent restore workspaces. The checked immutable-cache adapter is
`adapters/duckdb/odoo_saved_data_projection.py`. Source company selection is
stored separately by `adapters/duckdb/odoo_saved_source_company.py` and its
`schema/odoo_saved_source_company.py` companion.

`domain/odoo_restore.py` compiles schema coverage, scalar and relationship
policies and effective row results. `web/composition/saved_odoo_restore.py`
applies them through destination matching, transfer order, review, preflight,
execution and read-back. It keeps the ordinary transfer path's identity and
authorization boundaries.

`application/destination_matching_service.py` owns both entry points' direct
existing-parent scope. `destination_match_scope_candidates` offers admitted
captured relationships; `domain/workspace/portable_identity.py` translates
protected parent origins into portable business references and scoped keys.
Saved profiles retain the selected scope under `settings_json.scopes`.
`application/transfer_execution_service.py` uses the existing execution dataset
scope and row business-scope contracts. This slice requires one selected,
unscoped parent already matched uniquely at the destination. Broader scope
shapes and explicit company aliases remain outside this implementation.

`application/odoo_saved_data_archive.py` authorizes portable exports and imports.
`adapters/odoo_saved_data_package.py` owns the authenticated encrypted container.
The DuckDB archive backend selects exact retained members;
`odoo_saved_data_archive_payload.py` validates semantic coverage, and
`odoo_saved_data_archive_import.py` gives imported data new local ownership and
encryption keys. No original credential or destination write approval is
transported.

The [developer Odoo workflow](../developer/workflow/02-odoo-data.md#saved-odoo-data-and-repeat-restoration)
owns route, retention, consistency, compatibility and verification details.

## Mapping, preparation, and review

| Code to find | Where to start |
| --- | --- |
| Mapping rules and their row and columnar implementations | `domain/mapping` defines portable rules. `domain/mapping/scalar_values.py` evaluates a row, and `domain/compiler/columnar_transformation.py` and `adapters/polars_transformation.py` compile and run columnar transformations. |
| Match data pages and save recovery | `web/presenters/mapping_view.py` builds the page. `web/presenters/mapping_journey.py` resolves task locations. `adapters/duckdb/mapping_repository.py` commits the mutation receipt with the change. |
| Duplicate identities, relationship issues, and required values | Start in `application/workspace/mapping` with `identity_group_review.py`, `relationship_choice_review.py`, `duplicate_rows.py`, and `required_values.py`. The protected identity and relationship reviews page indexed results from the current check. |
| Saved supporting choices and refreshed Odoo selection values | `application/supporting_mapping_guard.py` checks saved file-source choices. `SupportingReviewService` rechecks access after key renewal. `web/selection_choices.py` verifies a narrow metadata refresh before `adapters/duckdb/selection_choices.py` publishes it in the mapping transaction. |
| Exact joins and reusable lookup lists | `application/workspace/derived_entities.py` reviews and saves joins. `application/workspace/preparation/grouped_exact_join_preparation.py::reviewed_exact_join_output` resolves reviewed standalone joins and groups for source reads and preparation. `adapters/duckdb/grouped_exact_join_source.py` reads verified snapshots and produces bounded join pages. `adapters/duckdb/lookup_preparation_source.py` normalizes distinct lookup values and retains their source row membership. |
| Existing hierarchy parents | `domain/hierarchy.py` defines portable prefix and company rules. `application/hierarchy_parent_lookup.py` browses bounded full-path choices and rechecks selected paths exactly. `web/composition/hierarchy_parents.py` separates local capture and publication from Odoo company and parent reads; `target_readers.py::hierarchy_parent_connector` constructs the remote adapter. The mapping compiler and preflight engine preserve the reuse-only boundary. |
| Preparation publication and recovery | `application/workspace/preparation` owns transformation and quality decisions. `preparation_session_repository.py` holds the DuckDB publication boundary. `application/workspace/preparation/recovery.py` checks existing publication evidence so a completed job can be restored without spawning another worker. |
| Preparation threads and diagnostic evidence | `application/shared/preparation_runtime.py` applies and verifies the child-only Polars thread limit. `web/composition/preparation_performance.py` observes the supervisor-owned attempt and samples process resources. `web/preparation_performance_diagnostics.py` sanitizes that evidence, and `web/performance_bundle.py` builds its bounded diagnostic ZIP members. `scripts/benchmark_preparation_workers.py` compares fixed first and repeat preparations across thread settings. |
| Diagnostic cost attribution | `application/shared/io_performance.py` measures existing artifact operations. `application/shared/worker_startup.py` collects source-worker startup intervals. `application/workspace/preparation/fallback_performance.py` records predicted and observed dataset routes. `web/diagnostic_costs.py` measures recorder overhead in memory, and `web/performance_attribution.py` sanitizes the exported costs. |
| Bounded numeric checking | `adapters/duckdb/preparation_numeric_precision.py` streams numeric cells through the authoritative predicate and inserts failure batches. `domain/preparation/quality_budget.py` bounds retained finding evidence, and `application/shared/numeric_performance.py` observes actual work without retaining values. |
| Review counts and affected rows | `adapters/duckdb/quality_repository.py` pages the preparation cause index. `quality_review_details.py` resolves the requested rows from saved lineage and prepared data. `preflight_repository.py` publishes comparison decisions and the compact review together. |
| Final review substeps | `web/presenters/final_review_journey.py` resolves presentation-only step and view parameters. `summary.py` renders the active responsibility, and `navigation.py` keeps its sidebar location separate from evidence-derived readiness. |
| Business expectations | `BusinessIntentService` saves the expectations. `application/workspace/execution/business_review.py` reviews them against the comparison. The execution journal retains the exact approval for recovery. |
| Final review workbook | `domain/preflight/mapping_summary.py` describes field rules from the verified mapping. `adapters/artifacts/reporting.py` renders the workbook from the comparison manifest and its bound evidence. |
| Correction worklist before load readiness | `application/workspace/review/attention_export.py` captures current saved findings. `domain/preflight/attention_projection.py` supplies their shared correction advice, and `adapters/artifacts/attention_workbook.py` writes complete Excel sheets. `web/routers/attention_export.py` serves the download and its counts. |

For a remote file-source comparison,
[`target_readers.py`](../../src/impodo/web/composition/target_readers.py) builds
bounded requests for linked records outside the primary schema.
`SupportingLookupService.current_preflight_reference` can supply a field
contract saved under the previous read key for the same target, reader,
company context, and policy. The remote reader verifies the current key
against the saved schema and checks linked-model access and live metadata
before fetching records.

`web/routers/execution.py` reloads the full confirmed schedule before starting a
load. `adapters/duckdb/execution_repository.py` records the in-flight batch
before the Odoo call. After an uncertain result,
`application/workspace/execution/reconciliation.py` classifies exact read-back
before the load can resume. `domain/execution/load_control.py` governs stopping
and continuation after known rejections.

`application/workspace/execution/load_observation.py` carries a load attempt
through the worker, journal, and transport. `web/load_diagnostics.py` and
`web/load_diagnostic_bundle.py` build support explanations from the recorded
event and terminal histories without reopening the database or calling Odoo.

## Integrated runs

1. `application/run/setup_service.py` and `web/routers/run_fresh_data.py` guide
   Test and Production through fresh data selection and typed answers.
   `application/run/odoo_requirements.py` reads the pinned Recipe revisions
   together and combines the Odoo models, fields, and relationship paths.
2. `application/run/test_activation.py` or `production_activation.py` checks
   the exact plan and provisions one workspace per Recipe application. The run
   owns the shared target schema and reference bundle.
   `RecipeApplicationService` builds each workspace's
   mapping from the pinned revision and its selected datasets.
3. `application/run/target_matches.py` reviews target values for an application.
   `web/run_commands.py` starts eligible work in dependency order.
   `web/run_review.py` builds the progress view from bounded registry reads
   and current job snapshots. The workspace retains detailed preparation,
   comparison, execution, and reconciliation evidence. Verified reconciliation
   unlocks dependent work.
4. `CutoverPlanService` reads the exact Test evidence through
   `WorkspaceIntegratedQualificationEvidenceReader`. `CutoverPlanRepository`
   persists protected qualification evidence for the exact plan revision.
5. `ProductionCutoverService` uses the selected qualification to create a fresh
   setup with the latest data and a different same-major target. It can resume
   incomplete local materialization. Before constructing the writer,
   `web/routers/execution.py` calls
   `ProductionCutoverService.assert_execution_authority` to verify the
   selected qualification, current plan revision, target, and credential
   identities.

The [integrated run](../developer/contracts/integrated-run-lifecycle.md),
[Cutover plan](../developer/contracts/cutover-plan-lifecycle.md), and
[Production run](../developer/contracts/production-run-lifecycle.md) contracts
describe which changes invalidate these bindings.

## Runtime and storage boundaries

- **Local browser.** `web/composition/local_app.py` selects DuckDB repositories,
  artifact stores, workspace access, and Odoo factories. `service_graph.py`
  builds application services from their ports. `odoo_source_graph.py` wires
  source capture, provenance, and publication. `application_graph.py` assembles
  the graph into `WebContext`, and `http_app.py` installs the browser routes.
  `web/app.py` keeps the existing local import entry point.
- **Local operation lifetime.** `web/composition/page_reads.py` runs synchronous
  page reads and local commands in worker threads. Keep DuckDB access within
  the callback and close it before Odoo calls or background work.
  `preparation_job_manager.py` starts the separate preparation worker, which
  opens its own workspace stores. See the
  [operation scope rules](code-organization.md#local-browser-database-operation-scopes).
- **DuckDB schema.** `adapters/duckdb/schema/forward_upgrades.py` checks the
  store identity and supported migration path. Each store applies consecutive
  upgrades and validates its schema in one transaction.
  `adapters/duckdb/registry_transaction.py` owns registry commit and rollback.
  Application ports expose atomic operations instead of passing connections
  across layers.
- **Odoo.** `domain/odoo/compatibility.py` defines the shared policy for Odoo 19
  and Odoo 20. `domain/odoo/field_capabilities.py` classifies editable scalar
  values for source capture and destination transfer from native metadata.
  `web/composition/target_readers.py` and `target_writers.py`
  select scoped adapters. The adapters batch reads by model and use separate
  write authority. `application/workspace/execution/identity_lookups.py` resolves
  nested parent keys in groups and keeps numeric IDs within the target
  operation.
- **Linux server.** `web/composition/server_cli.py` selects the process role.
  `server_linux_web.py` serves health, login, account recovery, and bounded
  administration routes. `server_linux_migration.py` applies PostgreSQL
  migrations, and `server_linux_admin.py` runs first-operator bootstrap.
  The worker command refuses startup. See
  [security and infrastructure](security-and-infrastructure.md) for the
  runtime boundaries.
- **Server storage and access.** `adapters/secrets/linux_files.py` reads the
  protected file secrets. `adapters/postgresql` owns control-plane migrations,
  identity, sessions, catalogue, and coordination transactions.
  `application/shared/cross_store_coordinator.py` uses durable receipts to
  finalize operations and recover interruptions. See
  [server fact ownership](server-fact-ownership.md) for the division between
  PostgreSQL, DuckDB, and artifacts.

## Focused verification

| Boundary | Start with |
| --- | --- |
| Layers and imports | `tests/architecture/test_inventory.py` and `test_dependency_rules.py` |
| Project creation and source ownership | `tests/application/project/test_authoring.py` and `tests/application/data_version/test_source_packages.py` |
| Forward store upgrades | `tests/integration/duckdb/test_forward_upgrades.py` |
| Workspace access and bounded reads | `tests/integration/web/test_workspace_page_admission.py` |
| Preparation native threads and performance evidence | `tests/application/workspace/preparation/test_thread_runtime.py`, `tests/integration/web/test_preparation_performance_observation.py`, `tests/integration/web/test_preparation_performance_diagnostics.py`, and `tests/performance/test_preparation_worker_benchmark.py` |
| Remote comparison and linked reads | `tests/integration/odoo/test_target_readers.py` and `tests/domain/workspace/test_supporting_lookups.py` |
| Load evidence and correction | `tests/integration/web/test_load_diagnostics.py` and `test_correction_workflow.py` |
| Integrated run and qualification | `tests/application/run/test_integrated_recipe_runs.py` and `tests/application/cutover/test_qualification.py` |
| Production activation | `tests/application/run/test_production_rollout.py` and `tests/integration/web/test_production_readiness.py` |

The [scenario qualification runbook](../developer/runbooks/scenario-qualification.md)
maps the customer and manufacturing practice fixtures to their scripts,
independent expected results, and live-run requirements.
