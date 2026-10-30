"""Assemble the complete browser application from explicit service and runtime groups.

Service builders remain responsible for wiring application use cases from ports.
This module joins those groups without choosing a persistence technology, then
maps deployment-selected request, job, storage, and target adapters into the
shared :class:`impodo.web.context.WebContext`.
"""

from __future__ import annotations

from dataclasses import dataclass

from impodo.application.browser_queries import (
    BrowserQueryService,
    DerivedEntityQueryRepository,
    MappingQueryRepository,
    MappingSourceQueryRepository,
    QualityQueryRepository,
    SchemaQueryRepository,
    SourceQueryRepository,
    TransformationImpactQueryRepository,
    WorkspaceStateQueryRepository,
)
from impodo.application.data_version.inspection import (
    SourceCatalogRepository,
    SourceInspectionService,
    WorkspaceCatalogReader,
)
from impodo.application.data_version.intake import SourceIntakeService
from impodo.application.destination_matching_service import (
    DestinationCreateFieldStore,
)
from impodo.application.odoo_capture_publication_service import (
    OdooCapturePublicationService,
)
from impodo.application.odoo_connection_service import OdooConnectionTestService
from impodo.application.odoo_provenance_service import OdooProvenanceService
from impodo.application.odoo_source_capture_service import OdooSourceCaptureService
from impodo.application.project.ports import ScopedMigrationProjectQuery
from impodo.application.run.target_matches import RecipeTargetMatchService
from impodo.application.shared.artifacts import (
    GovernedArtifactStores,
    WorkspaceArtifactStore,
)
from impodo.application.shared.job_runtimes import (
    CorrectionJobRuntime,
    LoadJobRuntime,
    OdooCaptureJobRuntime,
    PreflightJobRuntime,
    PreparationJobRuntime,
    RecipeRunJobRuntime,
)
from impodo.application.shared.jobs import JobDispatcher
from impodo.application.shared.secrets import SecretStore
from impodo.application.supporting_lookup_service import (
    SupportingLookupRepositoryPort,
    SupportingLookupService,
)
from impodo.application.workspace.derived_entities import (
    DerivedEntityRepository,
    DerivedEntityWorkspaceService,
    DerivedSourceRepository,
    ExactJoinGroupEvaluator,
)
from impodo.application.workspace.mapping.field_catalog import (
    MappingFieldCatalogQueryRepository,
)
from impodo.application.workspace.navigation import (
    WorkspaceNavigationQueryService,
    WorkspaceNavigationRepository,
)
from impodo.application.workspace.preparation.recovery import (
    PreparationRecoveryRepository,
    PreparationRecoveryService,
)
from impodo.application.workspace.preparation.recovery import (
    WorkspaceStateReader as PreparationRecoveryWorkspaceReader,
)
from impodo.application.workspace.views import (
    WorkspaceOwnerRepository,
    WorkspaceOwnerViewService,
)
from impodo.domain.shared.access import AuthorizationPolicy
from impodo.web.context import (
    BrowserReadinessReader,
    ConnectionTester,
    DestinationMatchingReader,
    LocalTargetRuntime,
    ModelCatalogReader,
    OdooReadbackReaderFactory,
    OdooSourceCaptureFactory,
    OdooWriteExecutorFactory,
    ReadIdentityProbe,
    SchemaReader,
    SchemaReadSessionFactory,
    WebContext,
    WriteIdentityProbe,
)
from impodo.web.remote_connection import RemoteConnectionStatusService
from impodo.web.request_identity import RequestIdentityResolver

from ..project_listing import UnavailableProjectSummary
from .odoo_source_graph import OdooSourceServiceGraph
from .service_graph import (
    AuthoringServiceGraph,
    CorrectionServiceGraph,
    CutoverServiceGraph,
    ExecutionServiceGraph,
    FoundationServiceGraph,
    PreparationServiceGraph,
    RunServiceGraph,
)


@dataclass(frozen=True, slots=True)
class WorkflowSupportServiceGraph:
    """Hold browser-facing application services outside the stage graphs."""

    queries: BrowserQueryService
    workspace_views: WorkspaceOwnerViewService
    navigation: WorkspaceNavigationQueryService
    intake: SourceIntakeService
    inspections: SourceInspectionService
    derived_entities: DerivedEntityWorkspaceService
    supporting_lookups: SupportingLookupService
    recipe_target_matches: RecipeTargetMatchService
    preparation_recovery: PreparationRecoveryService
    odoo_source_capture: OdooSourceCaptureService
    odoo_capture_publication: OdooCapturePublicationService
    odoo_provenance: OdooProvenanceService
    odoo_connection_tests: OdooConnectionTestService


@dataclass(frozen=True, slots=True)
class BrowserQueryPorts:
    """Hold read ports used by the browser's deployment-neutral query facade."""

    workspace_states: WorkspaceStateQueryRepository
    sources: SourceQueryRepository
    derived_entities: DerivedEntityQueryRepository
    schemas: SchemaQueryRepository
    mappings: MappingQueryRepository
    mapping_field_catalogs: MappingFieldCatalogQueryRepository
    quality: QualityQueryRepository
    transformation_impacts: TransformationImpactQueryRepository
    mapping_sources: MappingSourceQueryRepository


@dataclass(frozen=True, slots=True)
class WorkflowSupportPorts:
    """Hold persistence ports required by browser-support application services."""

    queries: BrowserQueryPorts
    workspace_owners: WorkspaceOwnerRepository
    navigation: WorkspaceNavigationRepository
    workspace_catalogs: WorkspaceCatalogReader
    source_catalogs: SourceCatalogRepository
    derived_sources: DerivedSourceRepository
    derived_entities: DerivedEntityRepository
    supporting_lookups: SupportingLookupRepositoryPort
    preparation_recovery: PreparationRecoveryRepository
    recovery_workspace_states: PreparationRecoveryWorkspaceReader
    exact_join_groups: ExactJoinGroupEvaluator | None = None


@dataclass(frozen=True, slots=True)
class ApplicationServiceGraph:
    """Hold the complete deployment-neutral Impodo application graph."""

    foundation: FoundationServiceGraph
    authoring: AuthoringServiceGraph
    runs: RunServiceGraph
    preparation: PreparationServiceGraph
    execution: ExecutionServiceGraph
    cutover: CutoverServiceGraph
    corrections: CorrectionServiceGraph
    support: WorkflowSupportServiceGraph


@dataclass(frozen=True, slots=True)
class WebRequestDependencies:
    """Hold request-scoped identity and visibility adapters."""

    project_queries: ScopedMigrationProjectQuery
    identity_resolver: RequestIdentityResolver
    unavailable_projects: tuple[UnavailableProjectSummary, ...]


@dataclass(frozen=True, slots=True)
class WebJobRuntimes:
    """Hold process or durable job runtimes selected by the deployment."""

    recipe_runs: RecipeRunJobRuntime | None
    preparation: PreparationJobRuntime | None
    preflight: PreflightJobRuntime | None
    load: LoadJobRuntime | None
    corrections: CorrectionJobRuntime
    odoo_capture: OdooCaptureJobRuntime | None


@dataclass(frozen=True, slots=True)
class WebInfrastructureDependencies:
    """Hold cross-cutting storage, authorization, and dispatch adapters."""

    destination_create_fields: DestinationCreateFieldStore
    artifacts: WorkspaceArtifactStore
    authorization: AuthorizationPolicy
    jobs: JobDispatcher
    secret_store: SecretStore


@dataclass(frozen=True, slots=True)
class WebTargetDependencies:
    """Hold deployment-selected Odoo I/O and connection-status adapters."""

    connection_tester: ConnectionTester
    read_identity_probe: ReadIdentityProbe
    write_identity_probe: WriteIdentityProbe
    schema_reader: SchemaReader
    schema_read_session_factory: SchemaReadSessionFactory | None
    model_catalog_reader: ModelCatalogReader
    readiness_reader: BrowserReadinessReader | None
    destination_match_reader: DestinationMatchingReader
    source_capture_factory: OdooSourceCaptureFactory
    write_executor_factory: OdooWriteExecutorFactory
    readback_reader_factory: OdooReadbackReaderFactory
    local_target: LocalTargetRuntime | None
    remote_connections: RemoteConnectionStatusService


def build_workflow_support_service_graph(
    *,
    foundation: FoundationServiceGraph,
    authoring: AuthoringServiceGraph,
    runs: RunServiceGraph,
    execution: ExecutionServiceGraph,
    ports: WorkflowSupportPorts,
    artifacts: GovernedArtifactStores,
    jobs: WebJobRuntimes,
    odoo_sources: OdooSourceServiceGraph,
    connection_tester: ConnectionTester,
    read_identity_probe: ReadIdentityProbe,
) -> WorkflowSupportServiceGraph:
    """Wire browser-support services without selecting persistence adapters."""

    query_ports = ports.queries
    queries = BrowserQueryService(
        query_ports.workspace_states,
        query_ports.sources,
        query_ports.derived_entities,
        query_ports.schemas,
        query_ports.mappings,
        query_ports.mapping_field_catalogs,
        query_ports.quality,
        query_ports.transformation_impacts,
        query_ports.mapping_sources,
    )
    supporting_lookups = SupportingLookupService(
        ports.supporting_lookups,
        foundation.workspace_access,
    )
    return WorkflowSupportServiceGraph(
        queries=queries,
        workspace_views=WorkspaceOwnerViewService(
            ports.workspace_owners,
            foundation.workspace_access,
        ),
        navigation=WorkspaceNavigationQueryService(
            ports.navigation,
            execution.execution,
            preparation_jobs=jobs.preparation,
            load_jobs=jobs.load,
        ),
        intake=SourceIntakeService(
            foundation.workspace_states,
            artifacts,
            foundation.workspace_access,
        ),
        inspections=SourceInspectionService(
            ports.workspace_catalogs,
            ports.source_catalogs,
            artifacts,
            foundation.workspace_access,
        ),
        derived_entities=DerivedEntityWorkspaceService(
            ports.derived_sources,
            ports.derived_entities,
            foundation.workspace_access,
            artifacts,
            ports.exact_join_groups,
        ),
        supporting_lookups=supporting_lookups,
        recipe_target_matches=RecipeTargetMatchService(
            authoring.mapping_workspace,
            runs.run_planning,
            supporting_lookups,
        ),
        preparation_recovery=PreparationRecoveryService(
            ports.preparation_recovery,
            authoring.mapping_workspace,
            ports.recovery_workspace_states,
        ),
        odoo_source_capture=odoo_sources.capture,
        odoo_capture_publication=odoo_sources.publication,
        odoo_provenance=odoo_sources.provenance,
        odoo_connection_tests=OdooConnectionTestService(
            connection_tester,
            read_identity_probe,
        ),
    )


def build_application_service_graph(
    *,
    foundation: FoundationServiceGraph,
    authoring: AuthoringServiceGraph,
    runs: RunServiceGraph,
    preparation: PreparationServiceGraph,
    execution: ExecutionServiceGraph,
    cutover: CutoverServiceGraph,
    corrections: CorrectionServiceGraph,
    support: WorkflowSupportServiceGraph,
) -> ApplicationServiceGraph:
    """Join responsibility-named service groups without selecting adapters."""

    return ApplicationServiceGraph(
        foundation=foundation,
        authoring=authoring,
        runs=runs,
        preparation=preparation,
        execution=execution,
        cutover=cutover,
        corrections=corrections,
        support=support,
    )


def build_web_context(
    services: ApplicationServiceGraph,
    *,
    request: WebRequestDependencies,
    jobs: WebJobRuntimes,
    infrastructure: WebInfrastructureDependencies,
    target: WebTargetDependencies,
) -> WebContext:
    """Map one application graph and deployment adapters into the route context."""

    foundation = services.foundation
    authoring = services.authoring
    runs = services.runs
    preparation = services.preparation
    execution = services.execution
    support = services.support
    return WebContext(
        queries=support.queries,
        project_queries=request.project_queries,
        identity_resolver=request.identity_resolver,
        unavailable_projects=request.unavailable_projects,
        workspace_access=foundation.workspace_access,
        workspace_views=support.workspace_views,
        navigation=support.navigation,
        migration_projects=foundation.migration_projects,
        data_versions=foundation.data_versions,
        migration_runs=foundation.migration_runs,
        migration_run_target_setup=foundation.migration_run_target_setup,
        migration_workspaces=foundation.migration_workspaces,
        project_authoring=foundation.project_authoring,
        odoo_source_restarts=foundation.odoo_source_restarts,
        recipes=authoring.recipes,
        recipe_publication=authoring.recipe_publication,
        run_planning=runs.run_planning,
        cutover_plans=services.cutover.cutover_plans,
        test_runs=runs.test_runs,
        production_runs=runs.production_runs,
        run_setups=runs.run_setups,
        data_version_source_projection=authoring.data_version_source_projection,
        workspace_states=foundation.workspace_states,
        intake=support.intake,
        inspections=support.inspections,
        sources=authoring.source_workspace,
        derived_entities=support.derived_entities,
        schema_workspace=authoring.schema_workspace,
        mapping_workspace=authoring.mapping_workspace,
        business_intents=authoring.business_intents,
        matching_order=authoring.matching_order,
        supporting_lookups=support.supporting_lookups,
        recipe_target_matches=support.recipe_target_matches,
        recipe_run_jobs=jobs.recipe_runs,
        categorical_coverage=authoring.categorical_coverage,
        preparation=preparation.preparation,
        preparation_recovery=support.preparation_recovery,
        preparation_jobs=jobs.preparation,
        quality=preparation.quality,
        resolution=preparation.resolution,
        normalization=preparation.normalization,
        preflight=preparation.preflight,
        preflight_jobs=jobs.preflight,
        execution=execution.execution,
        transfer_execution=execution.transfer_execution,
        load_jobs=jobs.load,
        reconciliation=execution.reconciliation,
        fallout_workbooks=execution.fallout_workbooks,
        corrections=services.corrections.workflow,
        correction_jobs=jobs.corrections,
        transformation_impacts=authoring.transformation_impacts,
        row_inclusion_reviews=authoring.row_inclusion_reviews,
        odoo_source_capture=support.odoo_source_capture,
        odoo_capture_publication=support.odoo_capture_publication,
        odoo_capture_jobs=jobs.odoo_capture,
        odoo_provenance=support.odoo_provenance,
        destination_create_fields=infrastructure.destination_create_fields,
        artifacts=infrastructure.artifacts,
        authorization=infrastructure.authorization,
        jobs=infrastructure.jobs,
        secret_store=infrastructure.secret_store,
        connection_tester=target.connection_tester,
        read_identity_probe=target.read_identity_probe,
        write_identity_probe=target.write_identity_probe,
        schema_reader=target.schema_reader,
        schema_read_session_factory=target.schema_read_session_factory,
        model_catalog_reader=target.model_catalog_reader,
        readiness_reader=target.readiness_reader,
        destination_match_reader=target.destination_match_reader,
        source_capture_factory=target.source_capture_factory,
        write_executor_factory=target.write_executor_factory,
        readback_reader_factory=target.readback_reader_factory,
        local_target=target.local_target,
        odoo_connection_tests=support.odoo_connection_tests,
        remote_connections=target.remote_connections,
    )


__all__ = [
    "ApplicationServiceGraph",
    "BrowserQueryPorts",
    "WebInfrastructureDependencies",
    "WebJobRuntimes",
    "WebRequestDependencies",
    "WebTargetDependencies",
    "WorkflowSupportPorts",
    "WorkflowSupportServiceGraph",
    "build_application_service_graph",
    "build_web_context",
    "build_workflow_support_service_graph",
]
