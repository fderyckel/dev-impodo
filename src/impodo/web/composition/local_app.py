"""Assemble the local browser application and all concrete dependencies.

``create_local_app`` connects DuckDB repositories, filesystem artifacts,
application services, closed Odoo readers and writer, security middleware, and route
modules through :class:`impodo.web.context.WebContext`. Business rules belong
to the injected services and domain modules; this module owns construction and
local deployment choices only.

The load writer and post-write read-back reader remain separate from the
preflight connectors and are bound to the exact reviewed local-load preview.

See ``docs/architecture/python-code-map.md`` and
``tests/integration/web/test_project_setup.py``.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from impodo.adapters.duckdb.scoped_project_query import (
    LocalScopedMigrationProjectQuery,
)
from impodo.adapters.duckdb.scoped_workspace_access import (
    LocalScopedWorkspaceAccessQuery,
)
from impodo.adapters.jobs.inline import InlineJobDispatcher
from impodo.adapters.odoo.connectors import Json2Config
from impodo.adapters.odoo.local_reader import LocalOdooMetadataReader
from impodo.adapters.odoo.local_stack import LocalStackService
from impodo.application.shared.artifacts import GovernedArtifactStores
from impodo.application.shared.build_contract import (
    PROCESS_BUILD_CONTRACT,
    ApplicationBuildContract,
)
from impodo.application.shared.jobs import JobDispatcher
from impodo.application.shared.secrets import SecretStore, SecretStoreError
from impodo.domain.shared.access import (
    LOCAL_ACTOR,
    Actor,
    AuthorizationPolicy,
)
from impodo.domain.workspace.workbench import (
    OdooConnectionMode,
    WorkspaceState,
    WorkspaceStateError,
)
from impodo.web.composition.incompatible_project_storage import (
    prepare_incompatible_project_storage,
)
from impodo.web.composition.preparation_job_manager import (
    PreparationJobManager,
)
from impodo.web.composition.target_readers import (
    _open_schema_read_session,
    _probe_read_identity,
    _read_destination_match,
    _read_model_catalog,
    _read_schema,
    _test_connection,
)
from impodo.web.composition.target_writers import (
    _probe_write_identity,
    _readback_reader,
    _write_executor,
)

from ...adapters.artifacts.fallout_workbook import build_fallout_workbook
from ...adapters.correction_review_pipeline import NativeCorrectionReviewPipeline
from ...adapters.duckdb.advanced_coverage_repository import AdvancedCoverageRepository
from ...adapters.duckdb.correction_repository import CorrectionRepository
from ...adapters.duckdb.data_version_source_repository import (
    DataVersionOwnedSourceRepository,
)
from ...adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from ...adapters.duckdb.exact_join_group_evaluator import (
    DuckDbExactJoinGroupEvaluator,
)
from ...adapters.duckdb.execution_repository import ExecutionRepository
from ...adapters.duckdb.mapping_field_catalog_repository import (
    MappingFieldCatalogRepository,
)
from ...adapters.duckdb.mapping_repository import MappingRepository
from ...adapters.duckdb.matching_order_repository import MatchingOrderRepository
from ...adapters.duckdb.migration_run_planning_repository import (
    MigrationRunPlanningRepository,
)
from ...adapters.duckdb.migration_workspace_state_repository import (
    MigrationWorkspaceStateRepository,
)
from ...adapters.duckdb.navigation_repository import WorkspaceNavigationRepository
from ...adapters.duckdb.normalization_repository import NormalizationRepository
from ...adapters.duckdb.odoo_provenance_repository import OdooProvenanceRepository
from ...adapters.duckdb.preflight_repository import PreflightRepository
from ...adapters.duckdb.preparation_recovery_repository import (
    PreparationRecoveryRepository,
)
from ...adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from ...adapters.duckdb.quality_repository import QualityRepository
from ...adapters.duckdb.recipe_compilation_repository import (
    RecipeCompilationRepository,
)
from ...adapters.duckdb.recipe_quality_seed_repository import (
    RecipeQualitySeedRepository,
)
from ...adapters.duckdb.reconciliation_repository import ReconciliationRepository
from ...adapters.duckdb.row_inclusion_review_repository import (
    RowInclusionReviewRepository,
)
from ...adapters.duckdb.run_aware_advanced_coverage_repository import (
    RunAwareAdvancedCoverageRepository,
)
from ...adapters.duckdb.run_aware_schema_repository import (
    RunAwareSchemaRepository,
)
from ...adapters.duckdb.schema_repository import SchemaRepository
from ...adapters.duckdb.staging_repository import StagingRepository
from ...adapters.duckdb.supporting_lookup_repository import (
    SupportingLookupRepository,
)
from ...adapters.duckdb.transformation_impact_repository import (
    TransformationImpactRepository,
)
from ...adapters.odoo_source_capture import Json2OdooSourceCapture
from ...adapters.polars_transformation import PolarsTransformationAdapter
from ...adapters.protected_correction_store import ProtectedCorrectionStore
from ...adapters.protected_destination_create_fields import (
    ProtectedDestinationCreateFieldStore,
)
from ...adapters.protected_odoo_capture_filters import ProtectedOdooCaptureFilterStore
from ...adapters.protected_odoo_comparison import ProtectedOdooComparisonCodec
from ...adapters.protected_odoo_provenance import ProtectedOdooProvenanceCodec
from ...adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from ...application.correction_jobs import CorrectionJobManager
from ...application.odoo_capture_job_service import OdooCaptureJobManager
from ...application.odoo_capture_publication_service import (
    OdooCapturePublication,
)
from ...application.preflight_jobs import PreflightJobManager
from ...application.run.recipe_run_jobs import RecipeRunJobManager
from ...application.workspace.execution.load_jobs import LoadJobManager
from ...application.workspace_source_projection import (
    WorkspaceMappingSourceProjection,
)
from ..capability_builders import (
    build_foundation_capability,
    build_protected_run_capability,
)
from ..context import (
    BrowserReadinessReader,
    ConnectionTester,
    DestinationMatchingReader,
    LifecycleRouteContext,
    LocalTargetRuntime,
    ModelCatalogReader,
    OdooReadbackReaderFactory,
    OdooSourceCaptureFactory,
    OdooWriteExecutorFactory,
    ReadIdentityProbe,
    SchemaReader,
    SchemaReadSessionFactory,
    WriteIdentityProbe,
)
from ..diagnostics import (
    LocalDiagnosticRecorder,
)
from ..remote_connection import RemoteConnectionStatusService
from ..request_identity import RequestIdentityResolver
from ..routers.lifecycle import build_lifecycle_router
from ..run_commands import publish_load_progress, publish_preparation_progress
from ..security import (
    LoopbackSecurityMiddleware,
)
from ..target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    local_read_credential_binding_hash,
)
from .application_graph import (
    BrowserQueryPorts,
    WebInfrastructureDependencies,
    WebJobRuntimes,
    WebRequestDependencies,
    WebTargetDependencies,
    WorkflowSupportPorts,
    build_application_service_graph,
    build_web_context,
    build_workflow_support_service_graph,
)
from .correction_target import CredentialBackedCorrectionTargetReviewStage
from .http_app import install_http_application
from .local_identity import (
    LOCAL_ORGANIZATION_ID,
    LocalRequestIdentityResolver,
)
from .odoo_source_graph import (
    OdooSourcePorts,
    build_odoo_source_service_graph,
)
from .service_graph import (
    AuthoringSchemaPorts,
    PreparationResolutionPorts,
    build_authoring_service_graph,
    build_correction_service_graph,
    build_cutover_service_graph,
    build_execution_service_graph,
    build_foundation_service_graph,
    build_preparation_service_graph,
    build_run_service_graph,
)

DEFAULT_BROWSER_DATABASE_LOCK_WAIT_SECONDS = 2.0
CLIENT_CLOSED_REQUEST_STATUS_CODE = 499


def create_local_app(
    project_root: str | Path,
    *,
    expected_host: str = "testserver",
    launch_token: str | None = None,
    session_secret: str | None = None,
    secret_store: SecretStore | None = None,
    connection_tester: ConnectionTester | None = None,
    read_identity_probe: ReadIdentityProbe | None = None,
    write_identity_probe: WriteIdentityProbe | None = None,
    schema_reader: SchemaReader | None = None,
    schema_read_session_factory: SchemaReadSessionFactory | None = None,
    model_catalog_reader: ModelCatalogReader | None = None,
    readiness_reader: BrowserReadinessReader | None = None,
    destination_match_reader: DestinationMatchingReader | None = None,
    source_capture_factory: OdooSourceCaptureFactory | None = None,
    write_executor_factory: OdooWriteExecutorFactory | None = None,
    readback_reader_factory: OdooReadbackReaderFactory | None = None,
    actor: Actor = LOCAL_ACTOR,
    authorization: AuthorizationPolicy | None = None,
    request_identity_resolver: RequestIdentityResolver | None = None,
    artifact_store: GovernedArtifactStores | None = None,
    job_dispatcher: JobDispatcher | None = None,
    local_stack_service: LocalStackService | None = None,
    local_odoo_reader: LocalOdooMetadataReader | None = None,
    preparation_jobs_enabled: bool = True,
    preflight_jobs_enabled: bool = False,
    odoo_capture_jobs_enabled: bool = True,
    recipe_run_jobs_enabled: bool = True,
    load_jobs_enabled: bool = True,
    duckdb_lock_wait_timeout_seconds: float = (
        DEFAULT_BROWSER_DATABASE_LOCK_WAIT_SECONDS
    ),
    application_build_contract: ApplicationBuildContract = PROCESS_BUILD_CONTRACT,
    diagnostic_recorder: LocalDiagnosticRecorder | None = None,
) -> FastAPI:
    """Construct the loopback FastAPI application and its service graph.

    Production defaults use per-project DuckDB repositories, local artifact
    storage, the credential vault, inline jobs, closed read-only Odoo adapters,
    and the separate practical writer. Parameters expose the security, storage,
    job, reader, and writer seams so tests or another local composition can
    replace them without changing application/domain behavior.

    An injected diagnostic recorder adds privacy-safe lifecycle, request, and
    event-loop timing evidence. It does not change application decisions.

    The returned app keeps the assembled :class:`WebContext` in
    ``app.state.context`` and passes that same context to every router. This
    function opens no project and contacts no Odoo target while composing.
    """

    def local_source_capture_factory(
        workspace_state: WorkspaceState,
        api_key: str,
    ) -> Json2OdooSourceCapture:
        """Build the local JSON-2 capture adapter from one selected target."""

        if workspace_state.odoo_connection_mode is None:
            raise WorkspaceStateError(
                "Configure the Odoo target before source capture"
            )
        return Json2OdooSourceCapture(
            Json2Config(
                base_url=workspace_state.odoo_base_url,
                database=workspace_state.odoo_database,
                api_key=api_key,
                connection_mode=workspace_state.odoo_connection_mode.value,
            )
        )

    unavailable_projects = prepare_incompatible_project_storage(project_root)
    foundation = build_foundation_capability(
        project_root,
        artifact_store=artifact_store,
        lock_wait_timeout_seconds=duckdb_lock_wait_timeout_seconds,
    )
    foundation_database = foundation.foundation_database
    foundation_repository = foundation.foundation_repository
    correction_repository = CorrectionRepository(foundation_repository)
    database = foundation.workspace_database
    artifacts = foundation.artifacts
    workspace_state_repository = MigrationWorkspaceStateRepository(
        database,
        foundation_repository,
    )
    recipe_compilation_repository = RecipeCompilationRepository(database)
    derived_entity_repository = DerivedEntityRepository(database)
    workspace_mapping_sources = WorkspaceMappingSourceProjection(
        foundation_repository,
        derived_entity_repository,
    )
    source_repository = DataVersionOwnedSourceRepository(
        database,
        derived_entity_repository,
        workspace_mapping_sources,
        foundation=foundation_repository,
    )
    local_schema_repository = SchemaRepository(database)
    run_planning_repository = MigrationRunPlanningRepository(
        foundation_repository
    )
    schema_repository = RunAwareSchemaRepository(
        local_schema_repository,
        run_planning_repository,
    )
    mapping_repository = MappingRepository(database, workspace_mapping_sources)
    matching_order_repository = MatchingOrderRepository(
        database,
        workspace_mapping_sources,
    )
    supporting_lookup_repository = SupportingLookupRepository(database)
    mapping_field_catalog_repository = MappingFieldCatalogRepository(database)
    staging_repository = StagingRepository(
        database,
        artifacts,
        source_selections=source_repository,
    )
    preparation_session_repository = PreparationSessionRepository(
        database,
        artifacts,
    )
    local_advanced_coverage_repository = AdvancedCoverageRepository(database)
    advanced_coverage_repository = RunAwareAdvancedCoverageRepository(
        local_advanced_coverage_repository,
        run_planning_repository,
    )
    quality_repository = QualityRepository(database, workspace_state_repository)
    normalization_repository = NormalizationRepository(
        database,
        workspace_state_repository,
    )
    preflight_repository = PreflightRepository(database, workspace_state_repository)
    execution_repository = ExecutionRepository(database)
    reconciliation_repository = ReconciliationRepository(database)
    transformation_impact_repository = TransformationImpactRepository(database)
    row_inclusion_review_repository = RowInclusionReviewRepository(database)
    protected_runs = build_protected_run_capability(
        project_root,
        foundation_repository=foundation_repository,
        authorization=authorization,
        secret_store=secret_store,
    )
    resolved_authorization = protected_runs.authorization
    foundation_services = build_foundation_service_graph(
        foundation_repository,
        workspace_state_repository,
        resolved_authorization,
    )
    workspace_access = foundation_services.workspace_access
    request_workspace_access = LocalScopedWorkspaceAccessQuery(
        workspace_access,
        organization_id=LOCAL_ORGANIZATION_ID,
    )
    resolved_secret_store = protected_runs.secret_store
    cutover_plan_repository = protected_runs.cutover_plan_repository
    production_run_repository = protected_runs.production_run_repository
    test_run_repository = protected_runs.test_run_repository
    recipe_repository = protected_runs.recipe_repository
    odoo_provenance_repository = OdooProvenanceRepository(
        database,
        artifacts,
        protected_root=lambda workspace_id: (
            foundation_database.root
            / "artifacts"
            / "dv"
            / foundation_repository.get_migration_workspace(
                workspace_id
            ).data_version_id
            / "protected"
        ),
    )
    odoo_capture_filters = ProtectedOdooCaptureFilterStore(
        ProtectedProjectEvidenceStore(project_root, resolved_secret_store)
    )
    destination_create_fields = ProtectedDestinationCreateFieldStore(
        ProtectedProjectEvidenceStore(project_root, resolved_secret_store)
    )
    odoo_sources = build_odoo_source_service_graph(
        workspace_access=workspace_access,
        ports=OdooSourcePorts(
            capture_workspaces=workspace_state_repository,
            capture_selections=source_repository,
            capture_schemas=schema_repository,
            capture_filters=odoo_capture_filters,
            relationship_scopes=source_repository,
            provenance_workspaces=workspace_state_repository,
            provenance_selections=source_repository,
            provenance_store=odoo_provenance_repository,
            publication_selections=source_repository,
            publication_store=odoo_provenance_repository,
            source_artifacts=artifacts,
            secrets=resolved_secret_store,
            provenance_codec=ProtectedOdooProvenanceCodec(),
            comparison_codec=ProtectedOdooComparisonCodec(),
        ),
    )
    recipe_application_state = RecipeQualitySeedRepository(database, database)
    authoring_services = build_authoring_service_graph(
        foundation=foundation_services,
        foundation_repository=foundation_repository,
        authorization=resolved_authorization,
        workspaces=workspace_state_repository,
        sources=source_repository,
        mapping_sources=workspace_mapping_sources,
        schemas=cast(AuthoringSchemaPorts, schema_repository),
        mappings=mapping_repository,
        quality=quality_repository,
        derived_entities=derived_entity_repository,
        references=advanced_coverage_repository,
        recipe_parameters=recipe_compilation_repository,
        supporting_lookups=supporting_lookup_repository,
        downstream_invalidator=correction_repository,
        recipe_applications=recipe_application_state,
        row_inclusion=row_inclusion_review_repository,
        matching_order=matching_order_repository,
        transformation_impacts=transformation_impact_repository,
        recipes=recipe_repository,
        artifacts=artifacts,
        capture_filters=odoo_capture_filters,
    )
    data_version_source_projection = (
        authoring_services.data_version_source_projection
    )
    run_services = build_run_service_graph(
        foundation=foundation_services,
        authoring=authoring_services,
        planning_repository=run_planning_repository,
        test_run_repository=test_run_repository,
        production_run_repository=production_run_repository,
        cutover_plan_repository=cutover_plan_repository,
        authorization=resolved_authorization,
    )
    preparation_services = build_preparation_service_graph(
        workspaces=workspace_state_repository,
        sources=source_repository,
        derived_entities=derived_entity_repository,
        mappings=mapping_repository,
        staging=staging_repository,
        sessions=preparation_session_repository,
        artifacts=artifacts,
        authorization=workspace_access,
        quality_repository=quality_repository,
        normalization_repository=normalization_repository,
        resolution_repository=cast(
            PreparationResolutionPorts,
            advanced_coverage_repository,
        ),
        preflight_repository=preflight_repository,
        schemas=schema_repository,
        columnar_transformations=PolarsTransformationAdapter(),
        odoo_provenance=odoo_sources.provenance,
        recipe_quality=recipe_application_state,
    )
    preflight = preparation_services.preflight

    def current_read_credential_binding(
        workspace_state: WorkspaceState,
        actor: Actor,
    ) -> str:
        credential_owner = context.target_credential_workspace(
            workspace_state.workspace_id,
            workspace_state=workspace_state,
            actor=actor,
        )
        if credential_owner.odoo_connection_mode is OdooConnectionMode.LOCAL:
            return local_read_credential_binding_hash(credential_owner)
        if credential_owner.odoo_connection_mode is not OdooConnectionMode.REMOTE:
            return ""
        try:
            credential = get_target_credential(
                resolved_secret_store,
                credential_owner,
                TargetCredentialRole.READ,
            )
        except SecretStoreError:
            return ""
        return credential.binding_hash if credential is not None else ""

    execution_services = build_execution_service_graph(
        workspaces=workspace_state_repository,
        preflight=preflight,
        journal=execution_repository,
        reconciliation_results=reconciliation_repository,
        sources=source_repository,
        staging=staging_repository,
        schemas=schema_repository,
        artifacts=artifacts,
        authorization=workspace_access,
        provenance=odoo_sources.provenance,
        current_read_credential_binding=current_read_credential_binding,
        fallout_workbook_renderer=build_fallout_workbook,
    )
    cutover_services = build_cutover_service_graph(
        foundation=foundation_services,
        preparation=preparation_services,
        execution=execution_services,
        run_planning=run_planning_repository,
        repository=cutover_plan_repository,
        mappings=mapping_repository,
        staging=staging_repository,
        quality=quality_repository,
        authorization=resolved_authorization,
    )
    preparation_jobs = (
        PreparationJobManager(
            project_root,
            build_contract=application_build_contract,
            diagnostic_recorder=diagnostic_recorder,
        )
        if preparation_jobs_enabled
        else None
    )
    preflight_jobs = PreflightJobManager() if preflight_jobs_enabled else None

    def accept_odoo_capture_publication(
        workspace_id: str,
        publication: OdooCapturePublication,
        publication_actor: Actor,
    ) -> None:
        data_version_source_projection.accept_odoo_capture(
            workspace_id,
            publication.source_selection,
            publication.source_snapshots,
            publication.manifests,
            actor=publication_actor,
        )

    odoo_capture_jobs = (
        OdooCaptureJobManager(
            odoo_sources.publication,
            accept_publication=accept_odoo_capture_publication,
        )
        if odoo_capture_jobs_enabled
        else None
    )
    recipe_run_jobs = RecipeRunJobManager() if recipe_run_jobs_enabled else None
    load_jobs = LoadJobManager() if load_jobs_enabled else None
    resolved_connection_tester = connection_tester or _test_connection
    resolved_read_identity_probe = read_identity_probe or _probe_read_identity
    resolved_schema_reader = schema_reader or _read_schema
    if schema_read_session_factory is not None:
        resolved_schema_read_session_factory = schema_read_session_factory
    elif schema_reader is None and read_identity_probe is None:
        resolved_schema_read_session_factory = _open_schema_read_session
    else:
        # Existing injected readers predate request-local sessions and may
        # capture a fresh target fingerprint on every call.  Keep those
        # integrations on the established full-read path unless they opt in
        # with an explicit session factory.
        resolved_schema_read_session_factory = None
    resolved_readback_reader_factory = (
        readback_reader_factory or _readback_reader
    )
    protected_correction_store = ProtectedCorrectionStore(
        ProtectedProjectEvidenceStore(project_root, resolved_secret_store)
    )
    correction_target = CredentialBackedCorrectionTargetReviewStage(
        workspace_states=workspace_state_repository,
        mappings=mapping_repository,
        secrets=resolved_secret_store,
        read_identity_probe=resolved_read_identity_probe,
        readback_reader_factory=resolved_readback_reader_factory,
    )
    correction_services = build_correction_service_graph(
        foundation=foundation_services,
        authoring=authoring_services,
        preparation=preparation_services,
        bindings=correction_repository,
        protected=protected_correction_store,
        mappings=mapping_repository,
        sources=source_repository,
        prepared=preparation_session_repository,
        preflight_records=preflight_repository,
        execution=execution_repository,
        reconciliations=reconciliation_repository,
        artifacts=artifacts,
        target=correction_target,
        review_pipeline_factory=NativeCorrectionReviewPipeline,
        authorization=resolved_authorization,
    )
    correction_jobs = CorrectionJobManager()
    job_runtimes = WebJobRuntimes(
        recipe_runs=recipe_run_jobs,
        preparation=preparation_jobs,
        preflight=preflight_jobs,
        load=load_jobs,
        corrections=correction_jobs,
        odoo_capture=odoo_capture_jobs,
    )
    resolved_destination_match_reader = destination_match_reader
    if resolved_destination_match_reader is None and readiness_reader is not None:

        def injected_destination_match_reader(
            workspace_state,
            _api_key,
            metadata_requests,
            record_requests,
        ):
            return readiness_reader(
                workspace_state,
                metadata_requests,
                record_requests,
            )

        resolved_destination_match_reader = injected_destination_match_reader

    resolved_identity_resolver = (
        request_identity_resolver or LocalRequestIdentityResolver(actor)
    )
    workflow_support = build_workflow_support_service_graph(
        foundation=foundation_services,
        authoring=authoring_services,
        runs=run_services,
        execution=execution_services,
        ports=WorkflowSupportPorts(
            queries=BrowserQueryPorts(
                workspace_states=workspace_state_repository,
                sources=source_repository,
                derived_entities=derived_entity_repository,
                schemas=schema_repository,
                mappings=mapping_repository,
                mapping_field_catalogs=mapping_field_catalog_repository,
                quality=quality_repository,
                transformation_impacts=transformation_impact_repository,
                mapping_sources=workspace_mapping_sources,
            ),
            workspace_owners=foundation_repository,
            navigation=WorkspaceNavigationRepository(database),
            workspace_catalogs=workspace_state_repository,
            source_catalogs=source_repository,
            derived_sources=source_repository,
            derived_entities=derived_entity_repository,
            supporting_lookups=supporting_lookup_repository,
            preparation_recovery=PreparationRecoveryRepository(database),
            recovery_workspace_states=workspace_state_repository,
            exact_join_groups=DuckDbExactJoinGroupEvaluator(database),
        ),
        artifacts=artifacts,
        jobs=job_runtimes,
        odoo_sources=odoo_sources,
        connection_tester=resolved_connection_tester,
        read_identity_probe=resolved_read_identity_probe,
    )
    application_services = build_application_service_graph(
        foundation=foundation_services,
        authoring=authoring_services,
        runs=run_services,
        preparation=preparation_services,
        execution=execution_services,
        cutover=cutover_services,
        corrections=correction_services,
        support=workflow_support,
    )
    context = build_web_context(
        application_services,
        request=WebRequestDependencies(
            project_queries=LocalScopedMigrationProjectQuery(
                foundation_repository,
                organization_id=LOCAL_ORGANIZATION_ID,
            ),
            identity_resolver=resolved_identity_resolver,
            unavailable_projects=unavailable_projects,
        ),
        jobs=job_runtimes,
        infrastructure=WebInfrastructureDependencies(
            destination_create_fields=destination_create_fields,
            artifacts=artifacts,
            authorization=resolved_authorization,
            jobs=job_dispatcher or InlineJobDispatcher(),
            secret_store=resolved_secret_store,
        ),
        target=WebTargetDependencies(
            connection_tester=resolved_connection_tester,
            read_identity_probe=resolved_read_identity_probe,
            write_identity_probe=write_identity_probe or _probe_write_identity,
            schema_reader=resolved_schema_reader,
            schema_read_session_factory=resolved_schema_read_session_factory,
            model_catalog_reader=model_catalog_reader or _read_model_catalog,
            readiness_reader=readiness_reader,
            destination_match_reader=(
                resolved_destination_match_reader or _read_destination_match
            ),
            source_capture_factory=(
                source_capture_factory or local_source_capture_factory
            ),
            write_executor_factory=write_executor_factory or _write_executor,
            readback_reader_factory=resolved_readback_reader_factory,
            local_target=LocalTargetRuntime(
                stack=local_stack_service or LocalStackService(),
                metadata_reader=(
                    local_odoo_reader or LocalOdooMetadataReader()
                ),
            ),
            remote_connections=RemoteConnectionStatusService(),
        ),
    )
    if preparation_jobs is not None:
        preparation_jobs.set_result_reader(
            lambda job, actor: context.preparation_recovery.current(
                job.workspace_id, job.workspace.mapping_content_hash, actor=actor,
            )
        )
        preparation_jobs.set_status_listener(
            lambda job, actor: publish_preparation_progress(
                context, job, actor=actor,
            )
        )
    if load_jobs is not None:
        load_jobs.set_status_listener(
            lambda job, actor: publish_load_progress(context, job, actor=actor)
        )

    def install_local_transport_security(app: FastAPI) -> None:
        app.add_middleware(
            SessionMiddleware,
            secret_key=session_secret or secrets.token_urlsafe(48),
            session_cookie="impodo_session",
            # The signing secret scopes this cookie to one launcher lifetime.
            max_age=None,
            same_site="strict",
            https_only=False,
        )
        app.add_middleware(
            LoopbackSecurityMiddleware,
            expected_host=expected_host,
        )

    return install_http_application(
        context,
        application_build_contract=application_build_contract,
        diagnostic_recorder=diagnostic_recorder,
        request_workspace_access=request_workspace_access,
        identity_resolver=resolved_identity_resolver,
        lifecycle_router=build_lifecycle_router(
            LifecycleRouteContext(
                launch_token=launch_token or secrets.token_urlsafe(32),
            )
        ),
        install_transport_security=install_local_transport_security,
    )
