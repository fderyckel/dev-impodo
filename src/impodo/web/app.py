"""Assemble the local browser application and all concrete dependencies.

Migration stages: cross-cutting A–K. Layer: composition root.

``create_local_app`` connects DuckDB repositories, filesystem artifacts,
application services, closed Odoo readers and writer, security middleware, and route
modules through :class:`impodo.web.context.WebContext`. Business rules belong
to the injected services and domain modules; this module owns construction and
local deployment choices only.

The Stage-J writer and Stage-K read-back reader remain separate from the
preflight connectors and are bound to the exact reviewed local-load preview.

See ``docs/architecture/python-code-map.md`` and
``tests/integration/web/test_project_setup.py``.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from impodo.domain.shared.access import (
    Actor,
    AuthorizationError,
    AuthorizationPolicy,
    LOCAL_ACTOR,
)
from ..application.browser_queries import BrowserQueryService
from ..application.correction_execution import CorrectionExecutionService
from ..application.correction_jobs import CorrectionJobManager
from ..application.correction_orchestration import (
    CorrectionAuthoringStageCoordinator,
    CorrectionMappingSeedService,
    CorrectionOriginPublisher,
    CorrectionReviewOrchestrator,
    CorrectionSuccessorService,
    CorrectionTargetReviewEvidence,
)
from ..application.correction_stages import (
    CallbackCorrectionTargetReviewStage,
    CurrentCorrectionMappingReviewStage,
    CurrentCorrectionPreparationReviewStage,
    CurrentCorrectionQualityReviewStage,
)
from ..application.correction_workflow import CorrectionWorkflowService
from ..application.workspace.mapping.categorical_coverage import (
    CategoricalCoverageService,
)
from ..application.workspace.mapping.service import MappingWorkspaceService
from ..application.workspace.preparation.normalization_service import NormalizationService
from ..application.odoo_capture_publication_service import OdooCapturePublicationService
from ..application.odoo_capture_job_service import OdooCaptureJobManager
from ..application.odoo_provenance_service import OdooProvenanceService
from ..application.odoo_source_capture_service import OdooSourceCaptureService
from ..application.preflight_service import PreflightService
from ..application.workspace.execution.service import ExecutionService
from ..application.workspace.execution.load_jobs import LoadJobManager
from ..application.workspace.execution.reconciliation import ReconciliationService
from ..application.recipe_compilation_service import RecipeCompiler
from ..application.migration_project_authoring_service import (
    MigrationProjectAuthoringService,
)
from impodo.application.shared.build_contract import ApplicationBuildContract, PROCESS_BUILD_CONTRACT
from ..application.run.planning_service import (
    MigrationRunPlanningService,
)
from ..application.cutover_plan_service import (
    CutoverPlanService,
    WorkspaceIntegratedQualificationEvidenceReader,
)
from ..application.production_cutover_service import ProductionCutoverService
from ..application.run.test_setup_service import TestRunSetupService
from ..application.recipe_application_service import RecipeApplicationService
from ..application.recipe_publication_service import RecipePublicationService
from ..application.workspace_source_projection import (
    WorkspaceMappingSourceProjection,
)
from ..application.workspace_data_version_source_service import (
    WorkspaceDataVersionSourceService,
)
from ..application.workspace.preparation.preparation_service import PreparationService
from impodo.web.composition.preparation_job_manager import (
    PreparationJobManager,
)
from ..application.workspace.preparation.quality_service import QualityService
from ..application.workspace.preparation.resolution_service import ResolutionService
from ..application.schema_workspace_service import SchemaWorkspaceService
from ..application.source_workspace_service import SourceWorkspaceService
from ..application.supporting_lookup_service import SupportingLookupService
from ..application.workspace.mapping.transformation_impact import (
    TransformationImpactService,
)
from impodo.application.shared.artifacts import GovernedArtifactStores
from impodo.application.workspace.derived_entities import DerivedEntityWorkspaceService
from impodo.application.data_version.intake import SourceIntakeService
from impodo.application.data_version.inspection import SourceInspectionService
from impodo.web.composition.incompatible_project_storage import prepare_incompatible_project_storage
from impodo.adapters.jobs.inline import InlineJobDispatcher
from impodo.application.shared.jobs import JobDispatcher
from impodo.adapters.odoo.local_reader import LocalOdooMetadataReader
from impodo.adapters.odoo.local_stack import LocalStackService
from ..adapters.duckdb.migration_workspace_state_repository import (
    MigrationWorkspaceStateRepository,
)
from ..adapters.duckdb.migration_run_planning_repository import (
    MigrationRunPlanningRepository,
)
from ..adapters.duckdb.run_aware_schema_repository import (
    RunAwareSchemaRepository,
)
from ..adapters.duckdb.run_aware_advanced_coverage_repository import (
    RunAwareAdvancedCoverageRepository,
)
from ..adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from ..adapters.duckdb.mapping_repository import MappingRepository
from ..adapters.duckdb.correction_repository import CorrectionRepository
from ..adapters.duckdb.mapping_field_catalog_repository import (
    MappingFieldCatalogRepository,
)
from ..adapters.duckdb.normalization_repository import NormalizationRepository
from ..adapters.duckdb.odoo_provenance_repository import OdooProvenanceRepository
from ..adapters.duckdb.preflight_repository import PreflightRepository
from ..adapters.duckdb.execution_repository import ExecutionRepository
from ..adapters.duckdb.reconciliation_repository import ReconciliationRepository
from ..adapters.duckdb.recipe_compilation_repository import (
    RecipeCompilationRepository,
)
from ..adapters.duckdb.recipe_quality_seed_repository import (
    RecipeQualitySeedRepository,
)
from ..adapters.duckdb.quality_repository import QualityRepository
from ..adapters.duckdb.schema_repository import SchemaRepository
from ..adapters.duckdb.data_version_source_repository import (
    DataVersionOwnedSourceRepository,
)
from ..adapters.duckdb.supporting_lookup_repository import (
    SupportingLookupRepository,
)
from ..adapters.duckdb.staging_repository import StagingRepository
from ..adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from ..adapters.duckdb.advanced_coverage_repository import AdvancedCoverageRepository
from ..adapters.duckdb.transformation_impact_repository import (
    TransformationImpactRepository,
)
from ..adapters.polars_transformation import PolarsTransformationAdapter
from ..adapters.correction_review_pipeline import NativeCorrectionReviewPipeline
from ..adapters.odoo_source_capture import Json2OdooSourceCapture
from ..adapters.protected_odoo_comparison import ProtectedOdooComparisonCodec
from ..adapters.protected_odoo_provenance import ProtectedOdooProvenanceCodec
from ..adapters.protected_correction_store import ProtectedCorrectionStore
from ..adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from impodo.domain.workspace.workbench import (
    WorkspaceState,
    OdooConnectionMode,
    WorkspaceStateCompatibilityError,
    WorkspaceStateError,
    WorkspaceStateNotFoundError,
    WorkspaceStateService,
)
from impodo.domain.workspace.models import MigrationWorkspaceState
from impodo.adapters.odoo.connectors import Json2Config
from impodo.application.data_version.source_packages import (
    DataVersionSourcePackageService,
    WorkspaceSourceProjectionService,
)
from ..application.data_version.service import DataVersionService
from ..application.project.service import MigrationProjectService
from ..application.run.service import MigrationRunService
from impodo.domain.run.setup import MigrationRunTargetSetupService
from ..application.workspace.service import MigrationWorkspaceService
from ..domain.recipe.models import RecipeError
from ..application.recipe.service import RecipeService
from ..application.odoo_connection_service import OdooConnectionTestService
from impodo.domain.project.foundation import (
    MigrationIdentifierConfusionError,
    MigrationNotFoundError,
)
from impodo.domain.correction_origin import CorrectionOriginError
from impodo.domain.execution.odoo_scope import OdooApiScope, OdooModelScope
from impodo.domain.serialization import content_hash
from impodo.application.workspace.access import (
    WorkspaceAccessContext,
    WorkspaceAccessService,
)
from impodo.application.workspace.views import WorkspaceOwnerViewService
from impodo.application.shared.secrets import SecretStore, SecretStoreError
from .context import (
    BrowserReadinessReader,
    ConnectionTester,
    DestinationMatchingReader,
    ModelCatalogReader,
    OdooWriteExecutorFactory,
    OdooReadbackReaderFactory,
    OdooSourceCaptureFactory,
    ReadIdentityProbe,
    SchemaReader,
    WriteIdentityProbe,
    WebContext,
)
from .capability_builders import (
    build_foundation_capability,
    build_protected_run_capability,
)
from .presenters.common import _render
from impodo.web.composition.target_readers import (
    _read_destination_match,
    _read_model_catalog,
    _read_schema,
    _probe_read_identity,
    _test_connection,
)
from .target_credentials import (
    TargetCredentialRole,
    get_target_credential,
    local_read_credential_binding_hash,
)
from impodo.web.composition.target_writers import _probe_write_identity, _readback_reader, _write_executor
from .routers.derived_entities import build_derived_entities_router
from .routers.destination_matching import build_destination_matching_router
from .routers.concepts import build_concepts_router
from .routers.lifecycle import build_lifecycle_router
from .routers.mapping import build_mapping_router
from .routers.normalization import build_normalization_router
from .routers.preflight import build_preflight_router
from .routers.execution import build_execution_router
from .routers.corrections import build_corrections_router
from .routers.preparation import build_preparation_router
from .routers.integrated_runs import build_integrated_runs_router
from .routers.cutover_plans import build_cutover_plans_router
from .routers.production_runs import build_production_runs_router
from .routers.migration_projects import build_migration_projects_router
from .routers.workspace_setup import build_workspace_setup_router
from .routers.quality import build_quality_router
from .routers.resolution import build_resolution_router
from .routers.schema import build_schema_router
from .routers.sources import build_sources_router
from .routers.summary import build_summary_router
from .routers.target import build_target_router
from .routers.transfer_destination import build_transfer_destination_router
from .routers.transfer_order import build_transfer_order_router
from .routers.transfer_review import build_transfer_review_router
from .routers.transfer_preflight import build_transfer_preflight_router
from .remote_connection import RemoteConnectionStatusService
from .run_review import publish_load_progress, publish_preparation_progress
from .security import (
    LoopbackSecurityMiddleware,
    WorkspaceAccessMiddleware,
)
from .diagnostics import (
    LocalDiagnosticRecorder,
    RequestDiagnosticsMiddleware,
    install_asyncio_exception_diagnostics,
    monitor_event_loop,
)
from .workspace_journeys import (
    WorkspaceJourney,
    enforce_workspace_journey,
    workspace_route_is_allowed,
)


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
    model_catalog_reader: ModelCatalogReader | None = None,
    readiness_reader: BrowserReadinessReader | None = None,
    destination_match_reader: DestinationMatchingReader | None = None,
    source_capture_factory: OdooSourceCaptureFactory | None = None,
    write_executor_factory: OdooWriteExecutorFactory | None = None,
    readback_reader_factory: OdooReadbackReaderFactory | None = None,
    actor: Actor = LOCAL_ACTOR,
    authorization: AuthorizationPolicy | None = None,
    artifact_store: GovernedArtifactStores | None = None,
    job_dispatcher: JobDispatcher | None = None,
    local_stack_service: LocalStackService | None = None,
    local_odoo_reader: LocalOdooMetadataReader | None = None,
    preparation_jobs_enabled: bool = True,
    odoo_capture_jobs_enabled: bool = True,
    load_jobs_enabled: bool = True,
    duckdb_lock_wait_timeout_seconds: float = 0.0,
    application_build_contract: ApplicationBuildContract = PROCESS_BUILD_CONTRACT,
    diagnostic_recorder: LocalDiagnosticRecorder | None = None,
) -> FastAPI:
    """Construct the loopback FastAPI application for migration Stages A–K.

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
    supporting_lookup_repository = SupportingLookupRepository(database)
    mapping_field_catalog_repository = MappingFieldCatalogRepository(database)
    staging_repository = StagingRepository(database, artifacts)
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
    protected_runs = build_protected_run_capability(
        project_root,
        foundation_repository=foundation_repository,
        authorization=authorization,
        secret_store=secret_store,
    )
    resolved_authorization = protected_runs.authorization
    workspace_access = WorkspaceAccessService(
        foundation_repository,
        resolved_authorization,
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
    odoo_provenance_service = OdooProvenanceService(
        workspace_state_repository,
        source_repository,
        odoo_provenance_repository,
        resolved_secret_store,
        workspace_access,
        ProtectedOdooProvenanceCodec(),
        ProtectedOdooComparisonCodec(),
    )
    odoo_source_capture = OdooSourceCaptureService(
        workspace_state_repository,
        source_repository,
        schema_repository,
        workspace_access,
    )
    odoo_capture_publication = OdooCapturePublicationService(
        odoo_source_capture,
        source_repository,
        odoo_provenance_service,
        odoo_provenance_repository,
        artifacts,
        workspace_access,
    )
    workspace_states = WorkspaceStateService(
        workspace_state_repository,
        workspace_access,
    )
    migration_projects = MigrationProjectService(
        foundation_repository,
        resolved_authorization,
    )
    data_versions = DataVersionService(
        foundation_repository,
        resolved_authorization,
    )
    migration_runs = MigrationRunService(
        foundation_repository,
        resolved_authorization,
    )
    migration_run_target_setup = MigrationRunTargetSetupService(
        foundation_repository,
        resolved_authorization,
    )
    migration_workspaces = MigrationWorkspaceService(
        foundation_repository,
        resolved_authorization,
    )
    source_packages = DataVersionSourcePackageService(
        foundation_repository,
        resolved_authorization,
    )
    project_authoring = MigrationProjectAuthoringService(
        migration_projects,
        data_versions,
        migration_runs,
        migration_workspaces,
        source_packages,
        workspace_states,
    )
    recipe_compiler = RecipeCompiler(
        workspace_mapping_sources,
        mapping_repository,
        schema_repository,
        quality_repository,
        derived_entity_repository,
        advanced_coverage_repository,
        recipe_compilation_repository,
    )
    recipes = RecipeService(
        recipe_repository,
        resolved_authorization,
    )
    recipe_publication = RecipePublicationService(
        recipe_repository,
        recipe_compiler,
        resolved_authorization,
    )
    data_version_source_projection = WorkspaceDataVersionSourceService(
        workspace_states,
        source_repository,
        data_versions,
        migration_workspaces,
        source_packages,
        WorkspaceSourceProjectionService(
            foundation_repository,
            resolved_authorization,
        ),
    )
    categorical_coverage = CategoricalCoverageService(
        source_repository,
        artifacts,
    )
    schema_workspace = SchemaWorkspaceService(
        workspace_state_repository,
        source_repository,
        schema_repository,
        workspace_access,
    )
    mapping_workspace = MappingWorkspaceService(
        workspace_mapping_sources,
        schema_repository,
        mapping_repository,
        workspace_access,
        categorical_coverage=categorical_coverage,
        supporting_lookups=supporting_lookup_repository,
        downstream_invalidator=correction_repository,
    )
    recipe_application_service = RecipeApplicationService(
        sources=workspace_mapping_sources,
        schemas=schema_repository,
        schema_workspace=schema_workspace,
        references=advanced_coverage_repository,
        preparation=derived_entity_repository,
        mappings=mapping_workspace,
        categorical=categorical_coverage,
        application_state=RecipeQualitySeedRepository(database),
    )
    run_planning = MigrationRunPlanningService(
        projects=migration_projects,
        data_versions=data_versions,
        recipes=recipes,
        repository=run_planning_repository,
        test_run_values=test_run_repository,
        source_packages=source_packages,
        source_projections=WorkspaceSourceProjectionService(
            foundation_repository,
            resolved_authorization,
        ),
        workspace_states=workspace_states,
        compiler=recipe_application_service,
        cutover_plans=cutover_plan_repository,
        authorization=resolved_authorization,
    )
    test_runs = TestRunSetupService(
        projects=migration_projects,
        data_versions=data_versions,
        runs=migration_runs,
        migration_workspaces=migration_workspaces,
        source_packages=source_packages,
        workspace_states=workspace_states,
        recipes=recipes,
        test_runs=test_run_repository,
        run_planning=run_planning,
        authorization=resolved_authorization,
    )
    production_runs = ProductionCutoverService(
        projects=migration_projects,
        data_versions=data_versions,
        runs=migration_runs,
        migration_workspaces=migration_workspaces,
        source_packages=source_packages,
        workspace_states=workspace_states,
        cutover_plans=cutover_plan_repository,
        production_runs=production_run_repository,
        run_planning=run_planning,
        authorization=resolved_authorization,
    )
    quality = QualityService(
        mapping_repository,
        source_repository,
        quality_repository,
    )
    normalization = NormalizationService(
        normalization_repository,
        workspace_access,
    )
    resolution = ResolutionService(advanced_coverage_repository, staging_repository)
    preparation = PreparationService(
        workspace_state_repository,
        source_repository,
        derived_entity_repository,
        mapping_repository,
        staging_repository,
        preparation_session_repository,
        artifacts,
        workspace_access,
        quality,
        normalization,
        PolarsTransformationAdapter(),
        resolution,
        odoo_provenance=odoo_provenance_service,
    )
    preflight = PreflightService(
        staging_repository,
        quality_repository,
        normalization_repository,
        mapping_repository,
        workspace_state_repository,
        source_repository,
        preflight_repository,
        artifacts,
        workspace_access,
        advanced_coverage_repository,
        schema_repository,
        odoo_provenance_service,
    )

    def current_read_credential_binding(workspace_state: WorkspaceState) -> str:
        test_credential_owner = test_runs.credential_workspace(
            workspace_state.workspace_id,
            actor=actor,
        )
        credential_owner = production_runs.credential_workspace(
            test_credential_owner.workspace_id,
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

    execution = ExecutionService(
        workspace_state_repository,
        preflight,
        execution_repository,
        workspace_access,
        require_remote_read_identity=True,
        require_remote_write_identity=True,
        current_read_credential_binding=current_read_credential_binding,
    )
    reconciliation = ReconciliationService(
        preflight,
        execution_repository,
        reconciliation_repository,
        workspace_access,
    )
    cutover_plans = CutoverPlanService(
        projects=migration_projects,
        data_versions=data_versions,
        run_planning=run_planning_repository,
        repository=cutover_plan_repository,
        evidence_reader=WorkspaceIntegratedQualificationEvidenceReader(
            mappings=mapping_repository,
            staging=staging_repository,
            quality=quality_repository,
            preflight=preflight,
            execution=execution_repository,
            reconciliation=reconciliation,
        ),
        authorization=resolved_authorization,
    )
    preparation_jobs = (
        PreparationJobManager(
            project_root,
            build_contract=application_build_contract,
        )
        if preparation_jobs_enabled
        else None
    )
    odoo_capture_jobs = (
        OdooCaptureJobManager(
            odoo_capture_publication,
            accept_publication=lambda workspace_id, publication, actor: (
                data_version_source_projection.accept_odoo_capture(
                    workspace_id,
                    publication.source_selection,
                    publication.source_snapshots,
                    publication.manifests,
                    actor=actor,
                )
            ),
        )
        if odoo_capture_jobs_enabled
        else None
    )
    load_jobs = LoadJobManager() if load_jobs_enabled else None
    resolved_connection_tester = connection_tester or _test_connection
    resolved_read_identity_probe = read_identity_probe or _probe_read_identity
    resolved_readback_reader_factory = (
        readback_reader_factory or _readback_reader
    )
    protected_correction_store = ProtectedCorrectionStore(
        ProtectedProjectEvidenceStore(project_root, resolved_secret_store)
    )

    def correction_target_capability(
        manifest,
        mapping,
        _datasets,
        successor_workspace_id,
        _actor,
    ) -> CorrectionTargetReviewEvidence:
        """Bind one fresh reader to exact targets and governed relationships."""

        successor_state = workspace_state_repository.get(
            successor_workspace_id
        )
        completed_state = workspace_state_repository.get(
            manifest.completed_workspace_id
        )
        credential = get_target_credential(
            resolved_secret_store,
            successor_state,
            TargetCredentialRole.READ,
        ) or get_target_credential(
            resolved_secret_store,
            completed_state,
            TargetCredentialRole.READ,
        )
        if credential is None:
            raise SecretStoreError(
                "Enter an Odoo read API key before reviewing the correction"
            )
        previous = mapping_repository.get_mapping_revision(
            manifest.completed_workspace_id
        )
        if previous is None:
            raise CorrectionOriginError("Completed correction rules are missing")
        fields_by_model: dict[str, set[str]] = {}
        lookup_fields_by_model: dict[str, set[str]] = {}
        for definition in (previous.definition, mapping.definition):
            for dataset in definition.datasets:
                fields_by_model.setdefault(dataset.target_model, set()).update(
                    field.target_field for field in dataset.fields
                )
                fields_by_model[dataset.target_model].update(
                    relationship.target_field
                    for relationship in dataset.relationships
                )
                for relationship in dataset.relationships:
                    resolver = relationship.resolver
                    if resolver.model and resolver.key_mappings:
                        lookup_fields = {
                            item.target_field
                            for item in (
                                *resolver.key_mappings,
                                *resolver.scope_mappings,
                            )
                        }
                        fields_by_model.setdefault(
                            resolver.model,
                            set(),
                        ).update(lookup_fields)
                        lookup_fields_by_model.setdefault(
                            resolver.model,
                            set(),
                        ).update(lookup_fields)
        scope = OdooApiScope(
            preview_hash=content_hash(
                {
                    "correction_origin": manifest.manifest_hash,
                    "corrected_mapping": mapping.definition.content_hash,
                }
            ),
            models=tuple(
                OdooModelScope(
                    model=model,
                    read_fields=tuple(sorted(fields)),
                    lookup_fields=tuple(
                        sorted(lookup_fields_by_model.get(model, set()))
                    ),
                )
                for model, fields in sorted(fields_by_model.items())
                if fields
            ),
        )
        models = tuple(item.model for item in scope.models)
        identity = resolved_read_identity_probe(
            successor_state,
            credential.secret,
            models,
        )
        if (
            identity.target_hash != manifest.target_hash
            or not set(models).issubset(identity.readable_models)
        ):
            raise CorrectionOriginError(
                "Current Odoo read access does not match the completed load"
            )
        try:
            reviewed_at = datetime.fromisoformat(
                identity.observed_at.replace("Z", "+00:00")
            )
        except ValueError as error:
            raise CorrectionOriginError(
                "Current Odoo read evidence has an invalid time"
            ) from error
        reader = resolved_readback_reader_factory(
            successor_state,
            credential.secret,
            scope,
        )
        return CorrectionTargetReviewEvidence(
            reader=reader,
            reader_scope_hash=scope.semantic_hash,
            read_credential_binding_hash=credential.binding_hash,
            read_identity=identity,
            reviewed_at=reviewed_at,
        )

    correction_stages = CorrectionAuthoringStageCoordinator(
        mapping=CurrentCorrectionMappingReviewStage(mapping_workspace),
        preparation=CurrentCorrectionPreparationReviewStage(
            preparation=preparation,
            sessions=preparation_session_repository,
            mappings=mapping_repository,
            sources=source_repository,
            artifacts=artifacts,
        ),
        quality=CurrentCorrectionQualityReviewStage(
            quality=quality,
            normalization=normalization,
        ),
        target=CallbackCorrectionTargetReviewStage(
            correction_target_capability
        ),
    )
    correction_origin_publisher = CorrectionOriginPublisher(
        correction_repository,
        protected_correction_store,
    )
    correction_successors = CorrectionSuccessorService(
        bindings=correction_repository,
        runs=migration_runs,
        workspaces=migration_workspaces,
        workspace_states=workspace_states,
        source_projections=data_version_source_projection.projections,
        target_setups=migration_run_target_setup,
        mapping_seeder=CorrectionMappingSeedService(
            schemas=schema_workspace,
            mappings=mapping_workspace,
        ),
    )
    corrections = CorrectionWorkflowService(
        bindings=correction_repository,
        protected=protected_correction_store,
        origin_publisher=correction_origin_publisher,
        successors=correction_successors,
        reviewer=CorrectionReviewOrchestrator(
            bindings=correction_repository,
            protected=protected_correction_store,
            pipeline=NativeCorrectionReviewPipeline(correction_stages),
        ),
        executor=CorrectionExecutionService(
            bindings=correction_repository,
            protected_store=protected_correction_store,
            execution=execution_repository,
            reconciliations=reconciliation_repository,
            authorization=resolved_authorization,
        ),
        runs=migration_runs,
        workspaces=migration_workspaces,
        mappings=mapping_repository,
        preparations=preparation_session_repository,
        preflight=preflight,
        preflight_repository=preflight_repository,
        executions=execution_repository,
        reconciliations=reconciliation_repository,
    )
    correction_jobs = CorrectionJobManager()
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

    context = WebContext(
        queries=BrowserQueryService(
            workspace_state_repository,
            source_repository,
            derived_entity_repository,
            schema_repository,
            mapping_repository,
            mapping_field_catalog_repository,
            quality_repository,
            transformation_impact_repository,
            workspace_mapping_sources,
        ),
        unavailable_projects=unavailable_projects,
        workspace_access=workspace_access,
        workspace_views=WorkspaceOwnerViewService(
            foundation_repository,
            workspace_access,
        ),
        migration_projects=migration_projects,
        data_versions=data_versions,
        migration_runs=migration_runs,
        migration_run_target_setup=migration_run_target_setup,
        migration_workspaces=migration_workspaces,
        project_authoring=project_authoring,
        recipes=recipes,
        recipe_publication=recipe_publication,
        run_planning=run_planning,
        cutover_plans=cutover_plans,
        test_runs=test_runs,
        production_runs=production_runs,
        data_version_source_projection=data_version_source_projection,
        workspace_states=workspace_states,
        intake=SourceIntakeService(
            workspace_states,
            artifacts,
            workspace_access,
        ),
        inspections=SourceInspectionService(
            workspace_state_repository,
            source_repository,
            artifacts,
            workspace_access,
        ),
        sources=SourceWorkspaceService(
            workspace_state_repository,
            source_repository,
            workspace_access,
            artifacts,
            schemas=schema_repository,
        ),
        derived_entities=DerivedEntityWorkspaceService(
            source_repository,
            derived_entity_repository,
            workspace_access,
        ),
        schema_workspace=schema_workspace,
        mapping_workspace=mapping_workspace,
        supporting_lookups=SupportingLookupService(
            supporting_lookup_repository,
            workspace_access,
        ),
        categorical_coverage=categorical_coverage,
        preparation=preparation,
        preparation_jobs=preparation_jobs,
        quality=quality,
        resolution=resolution,
        normalization=normalization,
        preflight=preflight,
        execution=execution,
        load_jobs=load_jobs,
        reconciliation=reconciliation,
        corrections=corrections,
        correction_jobs=correction_jobs,
        transformation_impacts=TransformationImpactService(
            workspace_state_repository,
            mapping_repository,
            source_repository,
            derived_entity_repository,
            transformation_impact_repository,
            artifacts,
            workspace_access,
        ),
        odoo_source_capture=odoo_source_capture,
        odoo_capture_publication=odoo_capture_publication,
        odoo_capture_jobs=odoo_capture_jobs,
        odoo_provenance=odoo_provenance_service,
        artifacts=artifacts,
        actor=actor,
        authorization=resolved_authorization,
        jobs=job_dispatcher or InlineJobDispatcher(),
        secret_store=resolved_secret_store,
        launch_token=launch_token or secrets.token_urlsafe(32),
        connection_tester=resolved_connection_tester,
        read_identity_probe=resolved_read_identity_probe,
        write_identity_probe=write_identity_probe or _probe_write_identity,
        schema_reader=schema_reader or _read_schema,
        model_catalog_reader=model_catalog_reader or _read_model_catalog,
        readiness_reader=readiness_reader,
        destination_match_reader=(
            resolved_destination_match_reader or _read_destination_match
        ),
        source_capture_factory=source_capture_factory or local_source_capture_factory,
        write_executor_factory=write_executor_factory or _write_executor,
        readback_reader_factory=resolved_readback_reader_factory,
        local_stack=local_stack_service or LocalStackService(),
        local_odoo_reader=local_odoo_reader or LocalOdooMetadataReader(),
        odoo_connection_tests=OdooConnectionTestService(
            resolved_connection_tester,
            resolved_read_identity_probe,
        ),
        remote_connections=RemoteConnectionStatusService(),
    )
    if preparation_jobs is not None:
        preparation_jobs.set_status_listener(
            lambda job: publish_preparation_progress(context, job)
        )
    if load_jobs is not None:
        load_jobs.set_status_listener(
            lambda job: publish_load_progress(context, job)
        )

    package_dir = Path(__file__).resolve().parent
    templates = Jinja2Templates(directory=package_dir / "templates")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        loop = asyncio.get_running_loop()
        previous_exception_handler = install_asyncio_exception_diagnostics(
            loop,
            diagnostic_recorder,
        )
        event_loop_monitor = None
        if diagnostic_recorder is not None:
            diagnostic_recorder.record_lifecycle(
                "application_started",
                build_version=(
                    f"contract-{application_build_contract.contract_version}"
                ),
            )
            event_loop_monitor = asyncio.create_task(
                monitor_event_loop(diagnostic_recorder),
                name="impodo-event-loop-monitor",
            )
        try:
            yield
        finally:
            if event_loop_monitor is not None:
                event_loop_monitor.cancel()
                await asyncio.gather(event_loop_monitor, return_exceptions=True)
            if diagnostic_recorder is not None:
                diagnostic_recorder.record_lifecycle("application_stopping")
            try:
                if context.preparation_jobs is not None:
                    context.preparation_jobs.shutdown()
                if context.odoo_capture_jobs is not None:
                    context.odoo_capture_jobs.shutdown()
                if context.load_jobs is not None:
                    context.load_jobs.shutdown()
                context.correction_jobs.shutdown()
            except BaseException as error:
                if diagnostic_recorder is not None:
                    diagnostic_recorder.record_lifecycle(
                        "application_shutdown_failed",
                        exception_class=type(error).__name__,
                    )
                raise
            else:
                if diagnostic_recorder is not None:
                    diagnostic_recorder.record_lifecycle("application_stopped")
            finally:
                loop.set_exception_handler(previous_exception_handler)

    app = FastAPI(
        title="Impodo",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.context = context
    app.state.build_contract = application_build_contract
    app.state.server = None
    app.state.templates = templates
    app.state.diagnostic_recorder = diagnostic_recorder
    app.mount(
        "/static",
        StaticFiles(directory=package_dir / "static"),
        name="static",
    )

    def trusted_job_context(
        path: str,
        workspace_id: str,
    ) -> WorkspaceAccessContext | None:
        """Reuse an already verified job packet without reopening the registry."""

        parts = path.strip("/").split("/")
        if len(parts) < 4 or parts[:2] != ["workspaces", workspace_id]:
            return None
        try:
            if (
                parts[2] == "preparation"
                and context.preparation_jobs is not None
            ):
                job = context.preparation_jobs.get(workspace_id, parts[3])
                return WorkspaceAccessContext(
                    project_id=job.workspace.project_id,
                    workspace_id=job.workspace.workspace_id,
                    data_version_id=job.workspace.data_version_id,
                    migration_run_id=job.workspace.migration_run_id,
                    recipe_application_id=job.workspace.recipe_application_id,
                    run_purpose=job.workspace.migration_run_purpose.value,
                )
            if (
                len(parts) >= 5
                and parts[2:4] == ["load", "progress"]
                and context.load_jobs is not None
            ):
                return context.load_jobs.get(workspace_id, parts[4]).access_context
            if (
                len(parts) >= 5
                and parts[2:4] == ["sources", "odoo-capture"]
                and context.odoo_capture_jobs is not None
            ):
                return context.odoo_capture_jobs.get(
                    workspace_id,
                    parts[4],
                ).access_context
        except LookupError:
            return None
        return None

    def workspace_route_policy(request, access_context):
        """Keep Recipe-run workspaces inside their owning run journey."""

        parts = request.url.path.strip("/").split("/")
        area = parts[2] if len(parts) >= 3 else ""
        trusted_job_path = (
            len(parts) >= 4 and parts[2] == "preparation"
        ) or (
            len(parts) >= 5 and parts[2:4] == ["load", "progress"]
        ) or (
            len(parts) >= 5 and parts[2:4] == ["sources", "odoo-capture"]
        )
        if trusted_job_path:
            # The access middleware already resolved these routes from their
            # verified job packet. Do not reopen a registry that the worker may
            # intentionally hold while publishing its result.
            return None
        workspace = context.migration_workspaces.repository.get_migration_workspace(
            access_context.workspace_id
        )
        if (
            workspace.state is MigrationWorkspaceState.CLOSED
            and area not in {"correction", "load"}
        ):
            binding = context.corrections.get(
                access_context.workspace_id,
                actor=context.actor,
            )
            request.session["flash"] = (
                "This completed workspace is historical evidence. Continue in "
                "its separate correction workspace."
            )
            return RedirectResponse(
                (
                    f"/workspaces/{access_context.workspace_id}/correction"
                    if binding is not None
                    else f"/projects/{access_context.project_id}"
                ),
                status_code=303,
            )

        if (
            access_context.recipe_application_id is not None
            and workspace_route_is_allowed(
                WorkspaceJourney.RECIPE_APPLICATION,
                request.url.path,
                access_context.workspace_id,
            )
        ):
            # Progress/status requests keep using their verified job packet and
            # do not reopen the registry merely to confirm an allowed route.
            return None
        if access_context.run_purpose is None:
            raise MigrationIdentifierConfusionError(
                "Workspace route ownership is missing its MigrationRun purpose"
            )
        return enforce_workspace_journey(
            request,
            access_context,
            access_context.run_purpose,
        )

    app.add_middleware(
        WorkspaceAccessMiddleware,
        access=context.workspace_access,
        actor=lambda: context.actor,
        trusted_context_resolver=trusted_job_context,
        route_policy=workspace_route_policy,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret or secrets.token_urlsafe(48),
        session_cookie="impodo_session",
        max_age=30 * 60,
        same_site="strict",
        https_only=False,
    )
    app.add_middleware(
        LoopbackSecurityMiddleware,
        expected_host=expected_host,
    )
    if diagnostic_recorder is not None:
        app.add_middleware(
            RequestDiagnosticsMiddleware,
            recorder=diagnostic_recorder,
        )

    @app.exception_handler(WorkspaceStateNotFoundError)
    async def project_not_found(_request: Request, _error: WorkspaceStateNotFoundError):
        return HTMLResponse("Project not found", status_code=404)

    @app.exception_handler(MigrationNotFoundError)
    async def migration_not_found(_request: Request, _error: MigrationNotFoundError):
        return HTMLResponse("Migration record not found", status_code=404)

    @app.exception_handler(MigrationIdentifierConfusionError)
    async def workspace_identity_mismatch(
        request: Request,
        _error: MigrationIdentifierConfusionError,
    ):
        message = (
            "Workspace not found"
            if request.url.path.startswith("/workspaces/")
            else "Migration record not found"
        )
        return HTMLResponse(message, status_code=404)

    @app.exception_handler(AuthorizationError)
    async def command_not_authorized(_request: Request, _error: AuthorizationError):
        return HTMLResponse("Not authorized", status_code=403)

    @app.exception_handler(RecipeError)
    async def recipe_error(_request: Request, error: RecipeError):
        return HTMLResponse(str(error), status_code=422)

    @app.exception_handler(WorkspaceStateCompatibilityError)
    async def project_incompatible(
        request: Request,
        error: WorkspaceStateCompatibilityError,
    ):
        return _render(
            request,
            "project_list.html",
            projects=context.migration_projects.list(actor=context.actor),
            unavailable_projects=context.unavailable_projects,
            error=str(error),
            status_code=409,
        )

    for router in (
        build_lifecycle_router(context.lifecycle_routes()),
        build_concepts_router(),
        build_migration_projects_router(context),
        build_integrated_runs_router(context),
        build_cutover_plans_router(context),
        build_production_runs_router(context),
        build_workspace_setup_router(context),
        build_target_router(context),
        build_transfer_destination_router(context),
        build_destination_matching_router(context),
        build_transfer_order_router(context),
        build_transfer_review_router(context),
        build_transfer_preflight_router(context),
        build_sources_router(context),
        build_schema_router(context),
        build_derived_entities_router(context),
        build_mapping_router(context),
        build_quality_router(context.quality_routes()),
        build_preparation_router(context),
        build_resolution_router(context),
        build_normalization_router(context),
        build_summary_router(context),
        build_preflight_router(context),
        build_execution_router(
            context,
            diagnostic_recorder=diagnostic_recorder,
        ),
        build_corrections_router(context),
    ):
        app.include_router(router)

    return app
