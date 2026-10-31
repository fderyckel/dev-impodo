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
| Render canonical workspace owners | `WorkspaceOwnerViewService` | one registry connection reads the explicit Project, workspace, DataVersion, run, and optional target view; source detail stays in its DataVersion store |
| Render source and schema review efficiently | `BrowserQueryService.get_source_page`, `SourceReviewPage`, `DerivedEntityWorkspaceService.preview_rules`, and `OdooSourceCaptureService.review_current_plans` | page workers reuse verified source inputs and evaluate rule cards without per-rule database reads; `adapters/duckdb/schema/structure.py` batches ordered-column inspection while every validation remains fresh |
| Reuse setup and credential status within a page | `RunSetupService.page_for_workspace`, `RunSetupPage`, and `RunOdooRequirementsUseCase.for_selection` | schema routing, requirements, and recovery share one authorized selection; active Test recovery counts blockers in SQL; the read-key dialog reuses safe status only for an identical credential owner and target |
| Classify shared workspace navigation | `WorkspaceNavigationFacts`, `WorkspaceNavigationQueryService`, `ExecutionPreviewSummary` | `WorkspaceNavigationRepository` reads scalar current evidence and freshness-checked aggregate Stage 3 issue counts in one workspace transaction; the presenter never materializes row artifacts or opens protected matching evidence |
| Supply mapping source contracts | `WorkspaceMappingSourceProjection` | bounded workspace source projection |
| Record and enforce business expectations | [`BusinessIntentService`](../../src/impodo/application/business_intent_service.py), [`BusinessIntent`](../../src/impodo/domain/workspace/business_intent.py), and [`business_intent_assessment.py`](../../src/impodo/domain/workspace/business_intent_assessment.py) | injected ports; local intent revision and audit publication; comparison-owned observations and assessments; journal-owned approval; bounded review queries without source or Odoo scans |
| Compile reusable meaning | `RecipeCompiler.compile_workspace` | reads current workspace evidence only |
| Publish optional Recipes | `RecipePublicationService` | `RecipeRepository`, protected Recipe store |
| Plan an integrated Test run | focused use cases under `application/run`; `MigrationRunPlanningService` is the stable facade | `MigrationRunPlanningRepository`, Project run routes |
| Materialize a fresh Recipe application | `RecipeApplicationService` | one isolated workspace and run-aware target projections |
| Resolve application order and resume steps | `application/run/progress.py` | `web/run_commands.py` coordinates commands and guarded milestones; `web/run_review.py` builds the view |
| Recover published preparation | `application/workspace/preparation/recovery.py` | `adapters/duckdb/preparation_recovery_repository.py` checks current publication bindings; the job manager restores a terminal snapshot without spawning a worker |
| Preserve Recipe meaning during run decisions | `domain/recipe/mapping_adaptation.py`, `MappingWorkspaceService` | `RecipeQualitySeedRepository` retains the compiler baseline and rebinds business checks |
| Review and confirm run-owned target values | `application/run/target_matches.py`, `RecipeTargetMatchService` | integrated run routes execute the service in the thread pool; `domain/workspace/supporting_lookups.py` shares ordered key serialization with capture |
| Compile and execute an Odoo-to-Odoo transfer | `TransferExecutionService`, `ExecutionService.execute_transfer`, and `ExecutionService.resume_transfer` | `web/routers/transfer_load.py` separates no-write preparation, explicit confirmation, background loading, read-back, and read-before-resume recovery; the existing execution journal binds the current transfer preflight |
| Qualify a governed profile scenario | `domain/scenarios` defines immutable inputs and compact results; `application/scenarios/ScenarioRunner` asserts checkpoints | `adapters/scenarios` loads contained fixtures, adapts the existing profile/preflight/writer/reconciliation services, and retains protected execution evidence; `web/composition/cli.py` exposes validation and explicit local execution |
| Recover an interrupted Odoo batch | `application/workspace/execution/reconciliation.py` assesses exact read-back; `ExecutionService.resume` and `resume_transfer` classify the same frozen schedule | `ExecutionRepository.record_batch_started` and `record_recovery` persist the checkpoint and report binding inside the existing row journal |
| Seal, review, execute, and verify completed-load correction intent | `domain/correction.py`, `domain/correction_origin.py`, `domain/correction_execution.py`, `application/correction_service.py`, `application/correction_orchestration.py`, `application/correction_stages.py`, `application/correction_workflow.py`, and `application/correction_execution.py` | `web/routers/corrections.py` and `CorrectionJobManager` present one focused resumable journey; native Polars/Parquet review emits sparse intent; exact protected IDs drive bounded reread, writes, and existing execution/reconciliation journals; one registry binding and whole-artifact hashes avoid row hashes |
| Version and qualify an integrated plan | `domain/cutover/models.py`, `CutoverPlanService` | `CutoverPlanRepository`, protected Project evidence, qualification routes |
| Run selected meaning with latest data | `ProductionCutoverService` | `ProductionRunRepository`, Production run routes, shared workspace engine |

`web/composition/local_app.py` composes these Project-first boundaries,
including the current Production coordinator.
`web/composition/service_graph.py` builds the deployment-neutral Project
foundation, source and Recipe authoring, Match data, Recipe application,
integrated run planning, run setup, preparation, reviewed loading, transfer,
reconciliation, fallout evidence, integrated Test qualification, and
completed-load correction services from injected dependencies. The local
composition currently supplies
DuckDB-backed repositories, protected run and Project evidence stores,
governed filesystem artifacts, workspace-scoped access, the Polars
transformation adapter, Odoo provenance, and the current credential-binding
callback. Odoo reader and writer factories stay at deployment-owned request or
worker boundaries.
`web/composition/application_graph.py` joins those responsibility-named groups
and constructs the remaining browser-facing application services from
`BrowserQueryPorts` and `WorkflowSupportPorts`. It then maps explicit request,
job, infrastructure, and target runtime bundles into `WebContext`.
`web/composition/odoo_source_graph.py` constructs Odoo source capture,
protected provenance, and atomic publication from the deployment-neutral
`OdooSourcePorts` bundle. Deployment compositions choose the repositories,
artifact and secret stores, filters, relationship scopes, and protected codecs.
`application/shared/job_runtimes.py` defines the structural job ports used by
that shared graph and context; local in-process managers and future durable
server adapters implement those ports. The local launch token stays outside
the shared context. A deployment that does not support local Odoo control can
omit `LocalTargetRuntime` instead of constructing a local adapter or branching
inside an application service.
`web/project_listing.py` owns the neutral unavailable-Project projection so the
shared context does not import the local DuckDB/filesystem compatibility
migration.
`application/run/ports.py` owns the repository contracts for integrated run
planning, Test setup, Production setup, Cutover plans, and the small shared
foundation surface those services consume. Run application code depends on
those contracts rather than on DuckDB repository classes.
`web/composition/http_app.py` installs the common route set around that
assembled graph, while `web/app.py` preserves the historical local import.
`web/composition/server_cli.py` is the single Linux-image process selector. It
exposes web, worker, migration, and administration roles. The bounded
`admin bootstrap` action is current; incomplete worker, login, and browser
administration runtimes still fail closed. The selector never calls the local
launcher.
[`deploy/Dockerfile`](../../deploy/Dockerfile) packages that entry point once
for every process role. [`deploy/compose.yaml`](../../deploy/compose.yaml)
defines the empty local rehearsal topology, and
[`deploy/local_rehearsal.py`](../../deploy/local_rehearsal.py) creates its
fictional ignored secret files without printing or replacing their values.
The local composition does not restore the superseded Recipe-root list,
creation, deletion, Test-application, Production-application, or qualification
services.

## Layer and capability index

| Code to find | Current path |
| --- | --- |
| Recognize Odoo versions and decide which operations are enabled | `domain/odoo/compatibility.py`; shared by connection, capture, comparison, Recipe, Production, and execution checks |
| Project, Data version, workspace, Recipe, run, Cutover, scenario, and built-in identity meaning | `domain/project`, `domain/data_version`, `domain/workspace`, `domain/recipe`, `domain/run`, `domain/cutover`, `domain/scenarios`, and `domain/identity`; `domain/identity/bootstrap.py` defines normalized first-operator input and secret-safe password-hash metadata, `domain/identity/authentication.py` defines generic login outcomes and throttle policy, `domain/identity/recovery.py` defines one-use token and generic reset contracts, `domain/identity/sessions.py` defines opaque handle, independent CSRF, expiry, elevation, current-view, and revocation contracts, `domain/identity/administration.py` defines tenant-scoped Organization, customer-account, event-page, and session-page commands, and `domain/identity/mfa.py` makes the privileged-account Internet release gate executable |
| Portable Mapping, Preparation, and Execution decisions | `domain/mapping`, `domain/compiler`, `domain/relationship_dependencies.py`, `domain/relationship_health.py`, `domain/matching_order.py`, `domain/preparation`, `domain/staging`, `domain/execution_snapshot.py`, and `domain/execution` |
| Recheck nested parent identities before loading or recovery | `application/workspace/execution/identity_lookups.py` resolves distinct governed ancestor keys in bounded model groups and gives the existing native adapters flat domains; numeric parent IDs remain inside the current target operation |
| Owner-qualified commands, queries, and ports | `application/project`, including `catalogue_service.py` for Organization-, principal-, capability-, and visibility-scoped catalogue access; `application/data_version`, `application/recipe`, `application/run`, `application/workspace`, `application/scenarios`, and `application/identity`; `PlatformAdministratorBootstrapService` builds one active operator, slow hash, and security event for atomic persistence, `ServerAuthenticationService` applies one generic rejection across Platform Administrator, Organization Administrator, and Customer User login, password verification, policy rehash, and bounded account and source throttles without creating a session, `AccountRecoveryService` keeps public reset requests opaque while binding token consumption to the password mutation, `ServerSessionService` creates, resolves, rotates after WebAuthn elevation, and revokes opaque server sessions without exposing stored digests, and `IdentityAdministrationService` separates platform Organization lifecycle from tenant-bound customer-user reads, activation, reset, event, and session operations |
| Cross-owner workflow coordinators and stable facades | Named modules directly below `application`, including Project authoring, Recipe compilation and publication, source projection, preflight, Cutover, and Production coordination. |
| Shared application ports and cross-store recovery | `application/shared`, including `jobs.py` for the deployment-neutral durable-job control port, `job_runtimes.py` for route-facing background-work lifecycles, `cross_store_coordinator.py` for receipt-driven finalization, durable recovery decisions, and named failure boundaries, `receipt_projection.py` for the deterministic bounded receipt projection, and `projection_reconciliation.py` for authorized bounded scans and receipt-only rebuilds; `domain/shared/jobs.py` defines the allowlisted server job, fenced attempt, progress, transition, cancellation, interruption, and result contracts without supplying a server runtime |
| DuckDB stores and forward-only schema handling | `adapters/duckdb` and `adapters/duckdb/schema`; `preparation_operation.py` stages reviewed scenario Preparation rows, commits the fenced workspace receipt, and publishes the immutable Parquet artifact through workspace schema version 19 |
| Artifact, protected-evidence, job, Odoo, scenario, and columnar implementations | `adapters/artifacts`, including `verification.py` for exact Workspace prepared-artifact hash and size checks; `adapters/protected_evidence`, `adapters/jobs`, `adapters/odoo`, and `adapters/scenarios`, plus named integration facades directly below `adapters` |
| Linux file-secret and PostgreSQL control-plane adapters | `adapters/secrets/linux_files.py` reads one bounded, root-owned direct child through no-follow file descriptors; `adapters/identity/scrypt_passwords.py` derives, verifies, and identifies stale salted memory-hard password hashes within an allowlisted work policy; `adapters/identity/secure_recovery_tokens.py` generates purpose-bound selector-plus-secret tokens and deployment-keyed identifier digests; `adapters/identity/secure_sessions.py` generates independent opaque handle and CSRF secrets and derives their domain-separated digests; `adapters/postgresql/control_schema.py` names the schema generation, current version, and least-privilege roles; `adapters/postgresql/startup.py` uses one short-lived, read-only Psycopg session to prove the runtime role, reachability, and exact schema generation; `adapters/postgresql/migrations.py` applies the immutable migration suffix under one transaction-level advisory lock and creates Organization-scoped catalogue, coordination, current-projection, first-operator, global-login, authentication-throttle, one-time recovery-token, tenant-bound opaque server-session, customer-account, administrator-assignment, tenant-event, and digest-only server-security-event tables; `adapters/postgresql/platform_bootstrap.py` serializes empty-state bootstrap and writes the operator, global login identity, password identity, and bounded event in one short-lived transaction; `adapters/postgresql/server_authentication.py` uses independent short-lived transactions for Platform Administrator, exact assigned Organization Administrator, or active Customer User credential lookup and outcome persistence, updates both throttle scopes, and compare-and-swaps policy rehashes without holding a transaction during scrypt; `adapters/postgresql/platform_recovery.py` reads issued-token dispatch metadata in one short transaction, atomically replaces reset tokens, binds one-use consumption to password and security revision changes, and revokes active sessions without holding a transaction during scrypt; `adapters/postgresql/platform_sessions.py` uses one short-lived transaction per server session create, resolve, WebAuthn rotation, or revocation and exposes no hard-delete method; `adapters/postgresql/identity_administration.py` applies Organization scope, actor locks, optimistic revisions, tenant-safe user reads, revision-bound link replacement, session revocation, and deterministic event and session pagination inside one short-lived transaction per call; `adapters/postgresql/project_catalogue.py` uses one short-lived transaction per Project catalogue call, scopes every query by Organization, and exposes no hard-delete method; `adapters/postgresql/coordination.py` uses one short-lived transaction per semantic reservation, immutable receipt, fenced Workspace lease operation, or bounded keyset reconciliation page, and prevents a late older projection from replacing a newer current pointer |
| Request handling and view construction | `web/routers` and `web/presenters` |
| Concrete runtime construction | `web/composition/local_app.py` chooses local adapters; `web/composition/server_cli.py` selects an explicit Linux process role after `server_config.py` validates that role's non-secret settings; `server_linux_migration.py` binds only the privileged migration secret and PostgreSQL migrator; `server_linux_admin.py` binds the protected runtime DSN and either an interactive password or one-time password file to the first-operator service; `server_storage.py` owns the platform-neutral writable-root policy, `server_linux_storage.py` observes Linux mount and write facts, and `server_linux_runtime.py` binds the Linux observer, runtime secret reader, and PostgreSQL startup inspector without supplying dependency fallbacks; `server_lifecycle.py` owns atomic privacy-safe readiness, `server_active_work.py` owns process-local work leases and the shared drain deadline, and `server_runtime.py` serializes startup, work admission, readiness withdrawal, and checkpoint completion; `server_control_app.py` exposes dependency-free liveness and generic readiness, `server_session_http.py` owns the independent `__Host-` handle and CSRF cookies plus host, Origin, framing, no-store, bounded request bodies, and related browser controls; `server_administration.py` owns global login, logout, and bounded list/detail reads; `server_administration_context.py` owns explicit browser ports and the complete synchronous request boundary; `server_organization_routes.py` owns platform provisioning and confirmed Organization lifecycle actions; `server_customer_routes.py` owns tenant-bound user mutations, session revocation, and bounded combined security history; `server_account_admission.py` bounds concurrent public account work, request rates, and retained source digests within one process; `server_account_routes.py` owns fragment-only activation/reset links, independent anonymous-form CSRF, issued-token dispatch before password hashing, and current-account views; `server_linux_administration.py` binds the deployment-keyed identifier digester and short-lived PostgreSQL services, `server_runtime_app.py` retains work leases through complete ASGI scopes and installs the browser policy, `server_uvicorn.py` binds exact proxy and signal-drain behavior, and `server_linux_web.py` assembles the current server process; `web/composition/service_graph.py` wires deployment-neutral Project foundation, authoring, Recipe application, integrated run setup, preparation, reviewed loading, transfer, reconciliation, fallout evidence, integrated Test qualification, and completed-load correction; `web/composition/odoo_source_graph.py` wires capture, provenance, and publication from explicit ports; `web/composition/application_graph.py` constructs browser-support services from explicit ports, joins all service groups, and performs the one shared mapping into `WebContext`; `web/composition/correction_target.py` resolves a deployment-owned secret and creates a scope-bound Odoo correction reader only when review work runs; other focused modules below `web/composition` and `web/capability_builders.py` own bounded construction; `web/app.py` is only a compatibility entry point |
| Privacy-safe local diagnostics and process recovery | `web/diagnostics.py` builds bounded evidence and redacted bundles, including process-local child logs when a shared file is locked; `web/server_supervisor.py` owns same-port child restart and the circuit breaker; `web/launcher.py` secures and binds the local runtime; `web/routers/lifecycle.py`, `web/static/server-recovery.js`, and `web/static/operation-state.js` own authenticated recovery and operation-aware liveness |
| Safe formula authoring feedback | `domain/recipe/value_rules.py` owns parsing; `web/mapping_formula_authoring.py` projects formula-free issues for the mapping route and browser |
| Reuse arithmetic preparation work | `domain/recipe/value_rules.py` compiles safe arithmetic into bounded Python instructions; `domain/staging/evaluator.py` binds them once per dataset, and `domain/mapping/scalar_values.py` supplies only referenced formula inputs |
| Replay native prepared evidence | `domain/staging/preparation_session.py` versions the canonical projection; `adapters/duckdb/native_prepared_projection.py` renders canonical reference bytes for new evidence and preserves historical encoding; `adapters/duckdb/preparation_stored_run_reader.py` verifies the bound artifacts and stored hashes |
| Gate downstream work on the current source version | `application/workspace/source_readiness.py` derives readiness from bounded navigation facts; `web/source_stage_gate.py` returns stale browser requests to the earliest source action; `web/run_commands.py` repeats the gate before preparation job creation |
| Recommend and select Odoo-source relationships | `domain/odoo_relationship_profiles.py` owns versioned standard recommendations; `domain/odoo_source_scope.py` owns metadata-derived discovery and safe inclusion; `domain/odoo_relationship_scope.py` owns immutable field-level decisions, canonical safe defaults, and recursive review completeness; `application/odoo_source_capture_service.py` gates assessment and freeze on that completeness; `adapters/duckdb/workspace_state_repository.py` stores decisions atomically with model scope; `web/presenters/odoo_source_scope.py`, `web/routers/sources.py`, and `web/templates/workspace_odoo_capture_selection.html` own compact exception review and the currently applicable **Advanced** matrix |
| Compile Odoo-transfer relationships | `domain/workspace/odoo_relationship_compilation.py` joins saved source edges to reviewed destination keys and derives canonical mapping resolvers; `application/destination_matching_service.py` checks that projection, and `application/transfer_order_service.py` passes it through `domain/relationship_dependencies.py` before shared scheduling |
| Match data identity, relationship health, work order, and live advisory check | `domain/relationship_health.py` owns aggregate relationship classification; `domain/matching_order.py` owns pure component ordering, typed identity facts, the isolated preference, and the combined aggregate check contract; `application/workspace/mapping/order_service.py` derives local advice, builds one bounded read plan, classifies protected exact-key evidence, and reconciles custom order; `application/workspace/mapping/health_issues.py` classifies navigation-safe Stage 3 issue counts; `adapters/duckdb/matching_order_repository.py` stores the versioned preference plus immutable aggregate and protected check rows; `web/routers/mapping.py`, the identity, relationship, and matching-health issue presenters, `web/presenters/mapping_view.py`, the shared issue summary, mapping health and order templates, and `web/static/mapping-order.js` own the explicit read-only action, safe progress, correction routes, and accessible queue without changing stable form indexes, semantic evidence, or execution order |
| Exact multi-table source enrichment | `domain/structural.py` owns exact-join execution, primary and added-key cardinality checks, physical lineage, deterministic structural identities, and private output policy; `domain/workspace/derived_entities.py` owns source-preparation contract v8 and final-only mapping visibility; `application/workspace/derived_entities.py` compiles and atomically reviews or saves one primary table with up to nine enrichments; `adapters/duckdb/exact_join_group_evaluator.py` owns narrow complete-data review; `application/workspace/preparation/grouped_exact_join_preparation.py` qualifies each reviewed group or unchanged physical table and appends all outputs to one durable session; its capability manifest makes volume advisory for qualified graphs; `adapters/duckdb/preparation_quality_index.py` accounts for shared and unused physical rows without whole-run reconstruction; `adapters/duckdb/grouped_exact_join_source.py` projects each verified snapshot once, performs the set-wise join, and returns bounded final-output pages without a cross-request connection cache; Recipe compilation and application preserve the private chain | `web/exact_join_forms.py` resolves the bounded server-owned key, completeness, and field matrices and recommends non-empty, non-conflicting outputs; grouped preview and save routes stay inside one local operation; `workspace_derived_entities.html` and `web/static/derived-entities.js` provide search, decision filters, bulk actions, qualified collisions, and the effective 200-field count; the saved view presents one final dataset while retaining the pairwise editor as advanced work |
| Guided combined source-column values | `domain/mapping/contracts.py` owns the portable ordered provider; `domain/mapping/scalar_values.py` is the row oracle; `domain/compiler/columnar_transformation.py` and `adapters/polars_transformation.py` own native execution; `web/presenters/mapping_forms.py` and `web/static/mapping-value-rules.js` own bounded authoring |
| Joined source choices and categorical validation | `application/workspace/mapping/logical_source_values.py` defines the bounded read contract; `adapters/duckdb/mapping_source_values.py` verifies reviewed join and snapshot evidence; `adapters/duckdb/grouped_exact_join_source.py` projects needed columns and aggregates bounded choices; `application/workspace/mapping/categorical_coverage.py` uses those values for generic Many2one matching, validation, and identity reads; the matching route closes its local operation before reading Odoo choices |
| One existing Odoo record for every row | `domain/mapping/contracts.py` owns the portable constant business reference and shared target-field order; `domain/mapping/validation/relationships.py` applies governed-reference policy; `domain/preparation/source.py`, `domain/compiler/columnar_transformation.py`, and `adapters/polars_transformation.py` own row and native reference production; `web/presenters/mapping_forms.py`, `web/templates/mapping/_relationship_catalog.html`, and `web/static/mapping-editor.js` own bounded many2one authoring; Recipe compilation and application preserve the rule without a source binding |
| Recoverable Match data mutation outcomes | `domain/mapping/mutations.py` defines receipts and structured conflicts; `adapters/duckdb/mapping_repository.py` commits receipts with mapping writes; `web/routers/mapping.py` and `web/static/mapping-editor.js` own read-back and recovery UX |
| Bounded Match data catalogue searches | `web/mapping_catalog_runtime.py` owns the content-keyed projection cache, per-editor newest-generation authority, and workspace single-flight gate; `web/static/mapping-catalogs.js` owns browser cancellation and current-result application |
| Server-rendered pages and page-owned browser behavior | `web/templates`, `web/templates/mapping`, and `web/static` |
| Focused evidence | `tests/architecture`, `tests/domain`, `tests/application`, `tests/integration`, `tests/e2e`, and `tests/performance` |
| Explicit non-discovered test builders and paths | `tests/support` |

## Maintenance boundaries

Flat reusable-list preparation belongs to
`adapters/duckdb/lookup_preparation_source.py::iter_lookup_batches`. It reads
only the lookup value and source ordinal columns, normalizes distinct values
once, and returns bounded output with complete physical membership to the
composed preparation session. Existing Many2one `required` and
`required_on_create` flags govern optional blanks in mapping contract v20.
The shared staging evaluator accepts all-blank optional references; preflight
enforces create-only requiredness. Recipe compilation retains the flags and
does not modify a source-preparation rule. Historical v19 mapping and v11
columnar program layouts retain their obsolete action only for exact decoding
and hashing.

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
| Incoming identity-group quality | `domain/preparation/quality.py` extracts symbolic dependency roles; `adapters/duckdb/preparation_identity_group_quality.py` projects narrow facts and traverses their graph | `application/workspace/preparation/bounded_quality.py` applies the domain quality policies to findings; temporary graph facts never change canonical evidence |
| Inspect complete preparation causes | `QualityCauseCount` and `QualityCauseImpact` expose value-free count and membership meaning | `quality_repository.py` materializes one publication-time cause index and pages it through `get_quality_cause_review_page`; `summary.py` renders authorized current-run detail without a request-time graph walk |
| Preparation bindings | `preparation_snapshot_bindings.py`, `preparation_derived_artifact_bindings.py`, `preparation_canonical_projection_bindings.py`, and `preparation_session_lifecycle.py` | each collaborator reuses the transaction opened for its one publication or lifecycle action |
| Mapping state and decisions | `domain/workspace/contracts.py`, `domain/mapping`, and `application/workspace/mapping` | a workspace owns mutable evidence; Recipe publication receives only portable rules |
| Preparation state and decisions | `domain/preparation` and `application/workspace/preparation` | `web/composition/preparation_job_manager.py` and `preparation_worker.py` own the local process runtime |
| Final review evidence | `domain/preflight` defines the portable report, complete-run value-free issue counts, and prepared-cell meaning; `application/preflight_service.py` binds current workspace evidence; `application/preflight_attempts.py` defines the value-free request and receipt; `application/preflight_timing.py` owns allowlisted publication measurements; `application/preflight_jobs.py` serializes observable read-only comparisons | `foundation_operation_intents.py` persists local attempt identity, coarse phases, and terminal receipts; `preflight_repository.py` atomically publishes bounded decision batches plus scalar and compact review projections and filters the existing decision page by stable issue code; `web/presenters/summary.py` adapts the counts into the shared issue journey; `adapters/artifacts/reporting.py` renders the manifest-authoritative workbook; `web/routers/preflight.py` reserves, enqueues, presents recoverable progress, and serves the artifact; `web/routers/execution.py` reads the compact review while confirmation reloads the complete snapshot |
| Matching review evidence | `domain/mapping` defines the checked revision, validation issues, and coverage; `TransformationImpactService.prepare_mapping_review_rows` reuses the Stage 3 evaluator over exact frozen file rows in memory | `adapters/artifacts/mapping_review.py` renders the non-authoritative row projection without publishing Stage 4 evidence or contacting Odoo; `web/routers/mapping.py` verifies Recipe lineage and creates and serves the exact-revision artifact |
| Execution state and decisions | `domain/run`, `domain/execution`, `application/run`, and `application/workspace/execution`; `ExecutionPreview` and `LoadJob` expose bounded read-only browser projections | `adapters/odoo` implements the target ports; `web/composition/target_readers.py` and `target_writers.py` select implementations, while `web/routers/execution.py` presents but never recalculates the frozen order |
| Safe write failure evidence and generated-name repair | `domain/execution/write_diagnostics.py` defines value-free failure context; `domain/execution/generated_name_repair.py` plans exact ID-bound spelling corrections from verified receipts without a writer | `adapters/odoo/write_diagnostics.py` validates bounded import-response evidence; the execution service persists it in the existing attempt JSON and the web router emits aggregate support codes without new database or Odoo calls |
| Completed-load correction decisions | `domain/correction.py`, `domain/correction_origin.py`, `domain/correction_execution.py`, `application/correction_service.py`, `application/correction_orchestration.py`, `application/correction_stages.py`, `application/correction_workflow.py`, and `application/correction_execution.py` | `web/composition/service_graph.py::build_correction_service_graph` wires the shared use cases from application-owned ports; `web/composition/correction_target.py` owns secret resolution, live identity verification, and one scope-bound reader; `adapters/polars_correction.py` emits sparse typed A/C differences from immutable prepared Parquet artifacts; `CorrectionReviewService` resolves distinct exact-existing many-to-one keys in bounded groups; `adapters/correction_review_pipeline.py` requires native programs; `web/routers/corrections.py` exposes safe counts and resumable progress only; the correction executor uses exact protected IDs and the existing scoped JSON-2, execution-journal, and reconciliation adapters without related-record writes; `adapters/duckdb/correction_repository.py` owns current pointers and verified successor completion |
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

`application/supporting_mapping_guard.py` checks saved file-source decisions
through injected ports at mapping, Recipe compilation and preparation
boundaries. `domain/workspace/supporting_closure.py` owns the pure source-aware
reconciliation and logical Recipe choice conversion. Spawned preparation
workers reuse their already loaded source and rule metadata. Odoo-source
relationship decisions remain with their existing compiler.

`application/recipe/odoo_requirements.py` extracts portable requirements from
already bulk-loaded, hash-verified revisions. The run-owned wrapper retains
authorization and selection reads. The result keeps exact revision and
relationship reasons while grouping compatible supporting requests.

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

`TestRunValues` keeps typed Recipe parameters and delivery control expectations
under one revision. Fresh data and the compiler share the domain control
normalizer. Activation reads those answers once and passes separate maps to
the existing compiler; the compiled baseline fixes accepted totals during
later mapping decisions.

`RecipeTargetMatchService` owns target-value review and confirmation. It reuses
one source coverage collection and caches each distinct captured lookup within
that review. It verifies complete composite and scope values without changing
their matching rule. The normal mapping service remains the authority for
validation and submission. Browser routes handle the form and navigation.

`RunSetupService` and `web/routers/run_fresh_data.py` share the guided source
journey. Production stores typed answers in its own `ProductionRunValues`
contract and registry table; Test retains its existing versioned answer format.
Readiness uses accepted answers rather than a second editable form.

1. The Project route creates one Test setup over exact Recipe revisions and
   explicit dependencies, then accepts one fresh Test DataVersion.
2. `RunOdooRequirementsUseCase.for_workspace` authorizes the run-owned
   query, bulk-reads the selected revisions, and unions their Odoo models,
   fields, and Recipe-owned relationship paths without contacting Odoo per
   Recipe. `RunSetupService` projects the selected Test revisions or qualified
   Production plan into that query while retaining separate aggregate owners.
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
8. `run_commands.py` starts only the first safe Test preparation. On run entry,
   it can recover that application's current published review. `run_review.py`
   builds ordered cards from bounded registry reads and one latest-snapshot
   pass per job manager. Status polling does not open a workspace.
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
3. `ProductionRunValuesUseCase` builds typed prompts from one batch of pinned
   Recipe revisions. Shared parameter and control validators normalize answers
   before access checks. `MigrationRunPlanningService.review_production_run` recompiles current
   bindings, parameters, controls, requirements, references, dependencies, and
   write ownership against the exact selected plan.
4. `MigrationRunPlanningRepository.activate_production_run` records one
   run-level target and requirement capture plus isolated applications in a
   restart-safe transaction.
5. The shared application materializer creates fresh workspace engines and
   mapping drafts using immutable DataVersion references. It copies no Test
   workspace evidence. The activation intent saves canonical answers and
   non-secret write identity evidence. `ProductionCutoverService.resume_activation`
   finishes local materialization from that request and skips completed mappings.
6. Readiness and run entry require the activation intent's final commit.
   Project overview queries setup completion in one batch and distinguishes it
   from run completion. `ProductionCutoverService.assert_execution_authority` rechecks selection,
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
| Final review | `PreflightService`, `PreflightJobManager` | `/workspaces/{workspace_id}/summary`, `/workspaces/{workspace_id}/preflight` |
| Load and reconcile | `ExecutionService`, `TransferExecutionService`, `LoadJobManager`, `ReconciliationService` | `/workspaces/{workspace_id}/load`, `/workspaces/{workspace_id}/transfer-load` |

Stage 2 matching-rule defaults start in
`domain/workspace/business_keys.py`. The schema presenter places one safe
recommendation in each unconfirmed form card, while
`SchemaWorkspaceService.govern_complete` enforces one confirmed rule for every
captured authoring model. Parent-owned records use ordinary portable key fields
plus a Many2one scope, such as BoM line Sequence within Parent BoM; no numeric
Odoo ID enters governance or Recipe meaning. The representative browser
journey in `tests/e2e/test_stage2_matching_rules.py` covers standard,
extended-standard, custom, and unresolved custom record types and proves that
review and confirmation do not add connector calls. The authenticated
`scripts/capture_stage2_matching_rule_screenshots.py` helper reproduces the
documented 1440 by 1024 review states from the same fictional captured schema.

## Query and Odoo performance

Run company selection starts in `web/routers/odoo_company.py` and the closed
`web/composition/odoo_company.py` adapter. `domain/odoo/company.py` defines the
protected target binding. `MigrationRunTargetSetup` owns its hash and owner;
`MigrationWorkspaceStateRepository` verifies the local protected copy for each
application workspace. The selection supplies one operation company without
injecting record-company values into mappings or portable Recipes.

`web/routers/matching_company_refresh.py` refreshes company-sensitive defaults
from Stage 3 between two bounded local operations. `web/matching_expectations.py`
projects the checked scope from metadata already read by the mapping presenter.
`web/routers/matching_expectations.py` saves explicit modes and count limits
through `BusinessIntentService`, then reassesses the saved aggregate through
the compact execution-review projection without reading full snapshots or rows.

`adapters/duckdb/schema/legacy_company.py` upgrades the known earlier
company-column layout under the existing transaction and exact final schema
validator. It preserves saved choices as protected historical evidence and
does not invent a run confirmation. Current stores retain the ordinary
single-validation operation path.

`domain/odoo/company_domains.py` interprets the closed company prefix supplied
by Odoo. `adapters/odoo/company_checks.py` groups exact captured IDs into
500-ID requests. `domain/preparation/company_references.py` applies the saved
compatibility result through the existing target-key index. Unknown custom
domains retain an explicit qualification limit and Odoo's write validation.

`MigrationFoundationRepository.list_projects`, run history, integrated
progress, and application issues use bounded registry projections. Review and
load obtains latest preparation and load snapshots for all Recipe workspaces
in one in-memory pass per manager. The
Project overview's single Recipe-publication readiness check may open its one
Authoring workspace; it must not grow into one workspace open per list row.
Mapping, preparation, and comparison may stream or batch rows, but they must
not issue one repository or Odoo request per row, field, or relationship.

Odoo adapters expose closed operations. Search and schema reads are qualified
for Odoo 19 and final Odoo 20 and are batched by model. Target keys are indexed
once, and write authority remains separate from read capability. Normal writes
and recovery support both majors; Odoo 20 Production remains disabled.

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
- `tests/application/run/test_production_values.py`
- `tests/integration/web/test_production_readiness.py`
- `tests/domain/recipe/test_representative_shapes.py`
- `tests/application/workspace/preparation/test_jobs.py`

The architecture tests and focused owner suites retain the fault-retry,
bounded-I/O, fixed-seed, browser, and complete-discovery contracts.
