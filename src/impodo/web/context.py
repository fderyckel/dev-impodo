"""Name the dependencies available to the shared browser routes.

:func:`impodo.web.composition.application_graph.build_web_context` constructs
``WebContext`` once from the shared application graph and deployment-selected
runtime adapters. Routes use the typed services and closed reader callables
here rather than constructing repositories or connectors. The context
contains no migration state of its own.

Target reads, the tightly scoped load writer, and the post-write read-back
reader remain separate boundaries.

See ``docs/architecture/python-code-map.md`` and
``tests/integration/web/test_project_setup.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

from impodo.application.data_version.inspection import SourceInspectionService
from impodo.application.data_version.intake import SourceIntakeService
from impodo.application.destination_matching_service import (
    DestinationCreateFieldStore,
)
from impodo.application.project.ports import ScopedMigrationProjectQuery
from impodo.application.shared.artifacts import WorkspaceArtifactStore
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
from impodo.application.workspace.access import WorkspaceAccessService
from impodo.application.workspace.derived_entities import DerivedEntityWorkspaceService
from impodo.application.workspace.navigation import WorkspaceNavigationQueryService
from impodo.application.workspace.views import WorkspaceOwnerViewService
from impodo.domain.execution.odoo_readback import OdooReadbackReader
from impodo.domain.execution.odoo_scope import OdooApiScope
from impodo.domain.execution.odoo_write import OdooWriteExecutor
from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
)
from impodo.domain.project.foundation import MigrationIdentifierConfusionError
from impodo.domain.run.setup import MigrationRunTargetSetupService
from impodo.domain.shared.access import Actor, AuthorizationPolicy
from impodo.domain.shared.models import (
    OdooReadIdentity,
    OdooWriteIdentity,
    TargetFingerprint,
)
from impodo.domain.workspace.workbench import (
    WorkspaceState,
    WorkspaceStateError,
    WorkspaceStateService,
)
from impodo.web.project_listing import UnavailableProjectSummary

from ..application.browser_queries import BrowserQueryService
from ..application.correction_workflow import CorrectionWorkflowService
from ..application.cutover_plan_service import CutoverPlanService
from ..application.data_version.service import DataVersionService
from ..application.fallout_workbook_service import FalloutWorkbookService
from ..application.migration_project_authoring_service import (
    MigrationProjectAuthoringService,
)
from ..application.odoo_capture_publication_service import OdooCapturePublicationService
from ..application.odoo_connection_service import OdooConnectionTestService
from ..application.odoo_provenance_service import OdooProvenanceService
from ..application.odoo_source_capture_service import (
    OdooSourceCapturePort,
    OdooSourceCaptureService,
)
from ..application.odoo_source_restart_service import OdooSourceRestartService
from ..application.preflight_service import PreflightService
from ..application.production_cutover_service import ProductionCutoverService
from ..application.project.service import MigrationProjectService
from ..application.recipe.service import RecipeService
from ..application.recipe_publication_service import RecipePublicationService
from ..application.run.planning_service import (
    MigrationRunPlanningService,
)
from ..application.run.service import MigrationRunService
from ..application.run.setup_service import RunSetupService
from ..application.run.target_matches import RecipeTargetMatchService
from ..application.run.test_setup_service import TestRunSetupService
from ..application.schema_workspace_service import SchemaWorkspaceService
from ..application.source_workspace_service import SourceWorkspaceService
from ..application.supporting_lookup_service import SupportingLookupService
from ..application.transfer_execution_service import TransferExecutionService
from ..application.workspace.execution.reconciliation import ReconciliationService
from ..application.workspace.execution.service import ExecutionService
from ..application.workspace.mapping.categorical_coverage import (
    CategoricalCoverageService,
)
from ..application.workspace.mapping.order_service import MatchingOrderService
from ..application.workspace.mapping.row_inclusion_review import (
    RowInclusionReviewService,
)
from ..application.workspace.mapping.service import MappingWorkspaceService
from ..application.workspace.mapping.transformation_impact import (
    TransformationImpactService,
)
from ..application.workspace.preparation.normalization_service import (
    NormalizationService,
)
from ..application.workspace.preparation.preparation_service import PreparationService
from ..application.workspace.preparation.quality_service import QualityService
from ..application.workspace.preparation.recovery import PreparationRecoveryService
from ..application.workspace.preparation.resolution_service import ResolutionService
from ..application.workspace.service import MigrationWorkspaceService
from ..application.workspace_data_version_source_service import (
    WorkspaceDataVersionSourceService,
)
from .remote_connection import RemoteConnectionStatusService
from .request_identity import RequestIdentityResolver

if TYPE_CHECKING:
    from impodo.adapters.odoo.local_reader import LocalOdooMetadataReader
    from impodo.adapters.odoo.local_stack import LocalStackService

ConnectionTester = Callable[[WorkspaceState, str], TargetFingerprint]

ReadIdentityProbe = Callable[
    [WorkspaceState, str, tuple[str, ...]],
    OdooReadIdentity,
]

WriteIdentityProbe = Callable[
    [WorkspaceState, str, OdooApiScope],
    OdooWriteIdentity,
]

SchemaReader = Callable[[WorkspaceState, str], MetadataSnapshot]


class SchemaReadSession(Protocol):
    """Read one request-local, monotonically growing Odoo schema scope."""

    def read_models(self, models: tuple[str, ...]) -> MetadataSnapshot:
        """Read metadata for exactly these newly reached models."""
        ...

    def verify_identity(
        self,
        models: tuple[str, ...],
    ) -> OdooReadIdentity | None:
        """Verify read access for the final complete model scope."""
        ...


SchemaReadSessionFactory = Callable[
    [WorkspaceState, str],
    SchemaReadSession,
]

ModelCatalogReader = Callable[[WorkspaceState, str], RecordSnapshot]

BrowserReadinessReader = Callable[
    [
        WorkspaceState,
        tuple[MetadataRequest, ...],
        tuple[RecordRequest, ...],
    ],
    tuple[MetadataSnapshot, RecordSnapshot],
]

DestinationMatchingReader = Callable[
    [
        WorkspaceState,
        str,
        tuple[MetadataRequest, ...],
        tuple[RecordRequest, ...],
    ],
    tuple[MetadataSnapshot, RecordSnapshot],
]

OdooWriteExecutorFactory = Callable[
    [WorkspaceState, str, OdooApiScope], OdooWriteExecutor
]
OdooReadbackReaderFactory = Callable[
    [WorkspaceState, str, OdooApiScope], OdooReadbackReader
]
OdooSourceCaptureFactory = Callable[[WorkspaceState, str], OdooSourceCapturePort]


@dataclass(slots=True)
class LocalTargetRuntime:
    """Hold adapters that exist only when this deployment supports local Odoo."""

    stack: LocalStackService
    metadata_reader: LocalOdooMetadataReader


@dataclass(slots=True)
class LifecycleRouteContext:
    """Expose the one launch boundary used by lifecycle routes."""

    launch_token: str


@dataclass(frozen=True, slots=True)
class QualityRouteContext:
    """Expose only the services used to edit mapping quality checks."""

    identity_resolver: RequestIdentityResolver
    queries: BrowserQueryService
    quality: QualityService
    transformation_impacts: TransformationImpactService


class CorrectionRouteContext(Protocol):
    """Expose only the services and target seams used by correction routes."""

    identity_resolver: RequestIdentityResolver
    queries: BrowserQueryService
    workspace_states: WorkspaceStateService
    corrections: CorrectionWorkflowService
    correction_jobs: CorrectionJobRuntime
    secret_store: SecretStore
    write_identity_probe: WriteIdentityProbe
    write_executor_factory: OdooWriteExecutorFactory
    readback_reader_factory: OdooReadbackReaderFactory


@dataclass(slots=True)
class WebContext:
    """Share one assembled set of services and boundary callables.

    Service fields expose browser use cases; ``queries`` provides read-only
    projections; target callables isolate read and practical write I/O; and
    authorization, artifacts, jobs, secrets, and deployment runtime services
    provide cross-cutting boundaries. Request actors are deliberately absent
    and must be resolved through ``identity_resolver``. The object is mutable
    only so composition roots and tests can replace explicitly injectable
    runtime seams.
    """

    queries: BrowserQueryService
    project_queries: ScopedMigrationProjectQuery
    identity_resolver: RequestIdentityResolver
    unavailable_projects: tuple[UnavailableProjectSummary, ...]
    workspace_access: WorkspaceAccessService
    workspace_views: WorkspaceOwnerViewService
    navigation: WorkspaceNavigationQueryService
    migration_projects: MigrationProjectService
    data_versions: DataVersionService
    migration_runs: MigrationRunService
    migration_run_target_setup: MigrationRunTargetSetupService
    migration_workspaces: MigrationWorkspaceService
    project_authoring: MigrationProjectAuthoringService
    odoo_source_restarts: OdooSourceRestartService
    recipes: RecipeService
    recipe_publication: RecipePublicationService
    run_planning: MigrationRunPlanningService
    cutover_plans: CutoverPlanService
    test_runs: TestRunSetupService
    production_runs: ProductionCutoverService
    run_setups: RunSetupService
    data_version_source_projection: WorkspaceDataVersionSourceService
    workspace_states: WorkspaceStateService
    intake: SourceIntakeService
    inspections: SourceInspectionService
    sources: SourceWorkspaceService
    derived_entities: DerivedEntityWorkspaceService
    schema_workspace: SchemaWorkspaceService
    mapping_workspace: MappingWorkspaceService
    matching_order: MatchingOrderService
    supporting_lookups: SupportingLookupService
    recipe_target_matches: RecipeTargetMatchService
    recipe_run_jobs: RecipeRunJobRuntime | None
    categorical_coverage: CategoricalCoverageService
    preparation: PreparationService
    preparation_recovery: PreparationRecoveryService
    preparation_jobs: PreparationJobRuntime | None
    quality: QualityService
    resolution: ResolutionService
    normalization: NormalizationService
    preflight: PreflightService
    preflight_jobs: PreflightJobRuntime | None
    execution: ExecutionService
    transfer_execution: TransferExecutionService
    load_jobs: LoadJobRuntime | None
    reconciliation: ReconciliationService
    fallout_workbooks: FalloutWorkbookService
    corrections: CorrectionWorkflowService
    correction_jobs: CorrectionJobRuntime
    transformation_impacts: TransformationImpactService
    row_inclusion_reviews: RowInclusionReviewService
    odoo_source_capture: OdooSourceCaptureService
    odoo_capture_publication: OdooCapturePublicationService
    odoo_capture_jobs: OdooCaptureJobRuntime | None
    odoo_provenance: OdooProvenanceService
    destination_create_fields: DestinationCreateFieldStore
    artifacts: WorkspaceArtifactStore
    authorization: AuthorizationPolicy
    jobs: JobDispatcher
    secret_store: SecretStore
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
    odoo_connection_tests: OdooConnectionTestService
    remote_connections: RemoteConnectionStatusService

    @property
    def local_stack(self) -> LocalStackService:
        """Return the local-stack adapter when this deployment provides one."""

        if self.local_target is None:
            raise WorkspaceStateError(
                "This Impodo deployment does not provide local Odoo controls"
            )
        return self.local_target.stack

    @local_stack.setter
    def local_stack(self, service: LocalStackService) -> None:
        if self.local_target is None:
            raise WorkspaceStateError(
                "This Impodo deployment does not provide local Odoo controls"
            )
        self.local_target.stack = service

    @property
    def local_odoo_reader(self) -> LocalOdooMetadataReader:
        """Return the local metadata adapter when this deployment provides one."""

        if self.local_target is None:
            raise WorkspaceStateError(
                "This Impodo deployment does not provide local Odoo reads"
            )
        return self.local_target.metadata_reader

    @local_odoo_reader.setter
    def local_odoo_reader(self, reader: LocalOdooMetadataReader) -> None:
        if self.local_target is None:
            raise WorkspaceStateError(
                "This Impodo deployment does not provide local Odoo reads"
            )
        self.local_target.metadata_reader = reader

    def quality_routes(self) -> QualityRouteContext:
        """Return the narrow dependency surface for mapping-quality routes."""

        return QualityRouteContext(
            identity_resolver=self.identity_resolver,
            queries=self.queries,
            quality=self.quality,
            transformation_impacts=self.transformation_impacts,
        )

    def target_credential_workspace(
        self,
        workspace_id: str,
        *,
        workspace_state: WorkspaceState | None = None,
        actor: Actor,
    ) -> WorkspaceState:
        """Project current target details onto the shared credential owner."""

        current = workspace_state or self.workspace_states.repository.get(workspace_id)
        if current.workspace_id != workspace_id:
            raise MigrationIdentifierConfusionError(
                "The target credential request changed workspace identity"
            )
        test_owner_id = self.test_runs.credential_workspace_id(
            workspace_id,
            actor=actor,
        )
        owner_id = self.production_runs.credential_workspace_id(
            test_owner_id,
            actor=actor,
        )
        if owner_id == current.workspace_id:
            return current
        return replace(current, workspace_id=owner_id)
