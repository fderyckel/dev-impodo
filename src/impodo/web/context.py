"""Name the dependencies available to every local browser route.

Migration stages: cross-cutting A–K. Layer: web dependency container.

:func:`impodo.web.app.create_local_app` constructs ``WebContext`` once and
passes it to each router builder. Routes use the typed services and closed
reader callables here rather than constructing repositories or connectors.
The context contains no migration state of its own.

Target reads, the tightly scoped Stage-J writer, and the Stage-K read-back
reader remain separate boundaries.

See ``docs/architecture/python-code-map.md`` and
``tests/integration/web/test_project_setup.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from impodo.domain.shared.access import Actor, AuthorizationPolicy
from ..application.browser_queries import BrowserQueryService
from ..application.workspace.mapping.categorical_coverage import (
    CategoricalCoverageService,
)
from ..application.workspace.mapping.service import MappingWorkspaceService
from ..application.workspace.preparation.normalization_service import NormalizationService
from ..application.odoo_capture_publication_service import OdooCapturePublicationService
from ..application.odoo_capture_job_service import OdooCaptureJobManager
from ..application.odoo_provenance_service import OdooProvenanceService
from ..application.odoo_source_capture_service import OdooSourceCapturePort
from ..application.preflight_service import PreflightService
from ..application.workspace.execution.service import ExecutionService
from ..application.workspace.execution.load_jobs import LoadJobManager
from ..application.workspace.execution.reconciliation import ReconciliationService
from ..application.migration_project_authoring_service import (
    MigrationProjectAuthoringService,
)
from ..application.run.planning_service import (
    MigrationRunPlanningService,
)
from ..application.cutover_plan_service import CutoverPlanService
from ..application.production_cutover_service import ProductionCutoverService
from ..application.run.test_setup_service import TestRunSetupService
from ..application.recipe_publication_service import RecipePublicationService
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
from impodo.application.shared.artifacts import WorkspaceArtifactStore
from impodo.domain.odoo.contracts import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
)
from impodo.application.workspace.derived_entities import DerivedEntityWorkspaceService
from impodo.application.data_version.intake import SourceIntakeService
from impodo.application.data_version.inspection import SourceInspectionService
from impodo.web.composition.incompatible_project_storage import UnavailableProjectSummary
from impodo.application.shared.jobs import JobDispatcher
from impodo.adapters.odoo.local_reader import (
    LocalOdooMetadataReader,
)
from impodo.domain.run.setup import MigrationRunTargetSetupService
from impodo.adapters.odoo.local_stack import LocalStackService
from impodo.domain.project.foundation import MigrationIdentifierConfusionError
from impodo.domain.shared.models import OdooReadIdentity, OdooWriteIdentity, TargetFingerprint
from impodo.domain.execution.odoo_write import OdooWriteExecutor
from impodo.domain.execution.odoo_readback import OdooReadbackReader
from impodo.domain.execution.odoo_scope import OdooApiScope
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStateService
from impodo.application.workspace.access import WorkspaceAccessService
from impodo.application.workspace.views import WorkspaceOwnerViewService
from ..application.data_version.service import DataVersionService
from ..application.project.service import MigrationProjectService
from ..application.run.service import MigrationRunService
from ..application.workspace.service import MigrationWorkspaceService
from ..application.recipe.service import RecipeService
from ..application.odoo_connection_service import OdooConnectionTestService
from impodo.application.shared.secrets import SecretStore
from .remote_connection import RemoteConnectionStatusService

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

ModelCatalogReader = Callable[[WorkspaceState, str], RecordSnapshot]

BrowserReadinessReader = Callable[
    [
        WorkspaceState,
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
class LifecycleRouteContext:
    """Expose the one launch boundary used by lifecycle routes."""

    launch_token: str


@dataclass(frozen=True, slots=True)
class QualityRouteContext:
    """Expose only the services used to edit mapping quality checks."""

    actor: Actor
    queries: BrowserQueryService
    quality: QualityService
    transformation_impacts: TransformationImpactService


@dataclass(slots=True)
class WebContext:
    """Share one assembled set of local services and boundary callables.

    Service fields expose browser use cases; ``queries`` provides read-only
    projections; target callables isolate read and practical write I/O; and
    actor, authorization, artifacts, jobs, secrets, and local-stack services
    provide cross-cutting boundaries. The object is mutable only so the local
    launcher and tests can replace explicitly injectable runtime seams.
    """

    queries: BrowserQueryService
    unavailable_projects: tuple[UnavailableProjectSummary, ...]
    workspace_access: WorkspaceAccessService
    workspace_views: WorkspaceOwnerViewService
    migration_projects: MigrationProjectService
    data_versions: DataVersionService
    migration_runs: MigrationRunService
    migration_run_target_setup: MigrationRunTargetSetupService
    migration_workspaces: MigrationWorkspaceService
    project_authoring: MigrationProjectAuthoringService
    recipes: RecipeService
    recipe_publication: RecipePublicationService
    run_planning: MigrationRunPlanningService
    cutover_plans: CutoverPlanService
    test_runs: TestRunSetupService
    production_runs: ProductionCutoverService
    data_version_source_projection: WorkspaceDataVersionSourceService
    workspace_states: WorkspaceStateService
    intake: SourceIntakeService
    inspections: SourceInspectionService
    sources: SourceWorkspaceService
    derived_entities: DerivedEntityWorkspaceService
    schema_workspace: SchemaWorkspaceService
    mapping_workspace: MappingWorkspaceService
    supporting_lookups: SupportingLookupService
    categorical_coverage: CategoricalCoverageService
    preparation: PreparationService
    preparation_jobs: PreparationJobManager | None
    quality: QualityService
    resolution: ResolutionService
    normalization: NormalizationService
    preflight: PreflightService
    execution: ExecutionService
    load_jobs: LoadJobManager | None
    reconciliation: ReconciliationService
    transformation_impacts: TransformationImpactService
    odoo_capture_publication: OdooCapturePublicationService
    odoo_capture_jobs: OdooCaptureJobManager | None
    odoo_provenance: OdooProvenanceService
    artifacts: WorkspaceArtifactStore
    actor: Actor
    authorization: AuthorizationPolicy
    jobs: JobDispatcher
    secret_store: SecretStore
    launch_token: str
    connection_tester: ConnectionTester
    read_identity_probe: ReadIdentityProbe
    write_identity_probe: WriteIdentityProbe
    schema_reader: SchemaReader
    model_catalog_reader: ModelCatalogReader
    readiness_reader: BrowserReadinessReader | None
    source_capture_factory: OdooSourceCaptureFactory
    write_executor_factory: OdooWriteExecutorFactory
    readback_reader_factory: OdooReadbackReaderFactory
    local_stack: LocalStackService
    local_odoo_reader: LocalOdooMetadataReader
    odoo_connection_tests: OdooConnectionTestService
    remote_connections: RemoteConnectionStatusService

    def lifecycle_routes(self) -> LifecycleRouteContext:
        """Return the narrow dependency surface for lifecycle routes."""

        return LifecycleRouteContext(launch_token=self.launch_token)

    def quality_routes(self) -> QualityRouteContext:
        """Return the narrow dependency surface for mapping-quality routes."""

        return QualityRouteContext(
            actor=self.actor,
            queries=self.queries,
            quality=self.quality,
            transformation_impacts=self.transformation_impacts,
        )

    def target_credential_workspace(
        self,
        workspace_id: str,
        *,
        workspace_state: WorkspaceState | None = None,
    ) -> WorkspaceState:
        """Project current target details onto the shared credential owner."""

        current = workspace_state or self.workspace_states.repository.get(workspace_id)
        if current.workspace_id != workspace_id:
            raise MigrationIdentifierConfusionError(
                "The target credential request changed workspace identity"
            )
        test_owner_id = self.test_runs.credential_workspace_id(
            workspace_id,
            actor=self.actor,
        )
        owner_id = self.production_runs.credential_workspace_id(
            test_owner_id,
            actor=self.actor,
        )
        if owner_id == current.workspace_id:
            return current
        return replace(current, workspace_id=owner_id)
