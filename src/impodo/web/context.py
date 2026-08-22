"""Name the dependencies available to every local browser route.

Migration stages: cross-cutting A–K. Layer: web dependency container.

:func:`impodo.web.app.create_local_app` constructs ``WebContext`` once and
passes it to each router builder. Routes use the typed services and closed
reader callables here rather than constructing repositories or connectors.
The context contains no migration state of its own.

Target reads, the tightly scoped Stage-J writer, and the Stage-K read-back
reader remain separate boundaries.

See ``docs/architecture/python-code-map.md`` and ``tests/test_web_app.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..access import Actor, AuthorizationPolicy
from ..application.browser_queries import BrowserQueryService
from ..application.categorical_coverage_service import CategoricalCoverageService
from ..application.mapping_workspace_service import MappingWorkspaceService
from ..application.normalization_service import NormalizationService
from ..application.odoo_capture_publication_service import OdooCapturePublicationService
from ..application.odoo_capture_job_service import OdooCaptureJobManager
from ..application.odoo_provenance_service import OdooProvenanceService
from ..application.odoo_source_capture_service import OdooSourceCapturePort
from ..application.preflight_service import PreflightService
from ..application.execution_service import ExecutionService
from ..application.reconciliation_service import ReconciliationService
from ..application.migration_project_authoring_service import (
    MigrationProjectAuthoringService,
)
from ..application.migration_run_planning_service import (
    MigrationRunPlanningService,
)
from ..application.cutover_plan_service import CutoverPlanService
from ..application.project_recipe_publication_service import (
    ProjectRecipePublicationService,
)
from ..application.workspace_data_version_source_service import (
    WorkspaceDataVersionSourceService,
)
from ..application.preparation_service import PreparationService
from ..application.preparation_job_service import PreparationJobManager
from ..application.quality_service import QualityService
from ..application.resolution_service import ResolutionService
from ..application.schema_workspace_service import SchemaWorkspaceService
from ..application.source_workspace_service import SourceWorkspaceService
from ..application.supporting_lookup_service import SupportingLookupService
from ..application.transformation_impact_service import TransformationImpactService
from ..artifacts import ArtifactStore
from ..connectors import (
    MetadataRequest,
    MetadataSnapshot,
    RecordRequest,
    RecordSnapshot,
)
from ..derived_entities import DerivedEntityWorkspaceService
from ..intake import SourceIntakeService
from ..inspection import SourceInspectionService
from ..incompatible_project_storage import UnavailableProjectSummary
from ..jobs import JobDispatcher
from ..local_odoo_reader import (
    LocalOdooMetadataReader,
)
from ..local_stack import LocalStackService
from ..models import OdooReadIdentity, OdooWriteIdentity, TargetFingerprint
from ..odoo_writer import OdooWriteExecutor
from ..odoo_readback import OdooReadbackReader
from ..odoo_scope import OdooApiScope
from ..projects import WorkspaceState, ProjectService
from ..data_versions import DataVersionService
from ..migration_projects import MigrationProjectService
from ..migration_runs import MigrationRunService
from ..migration_workspaces import MigrationWorkspaceService
from ..project_recipes import ProjectRecipeService
from ..application.odoo_connection_service import OdooConnectionTestService
from ..secrets import SecretStore
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
    migration_projects: MigrationProjectService
    data_versions: DataVersionService
    migration_runs: MigrationRunService
    migration_workspaces: MigrationWorkspaceService
    project_authoring: MigrationProjectAuthoringService
    project_recipes: ProjectRecipeService
    recipe_publication: ProjectRecipePublicationService
    run_planning: MigrationRunPlanningService
    cutover_plans: CutoverPlanService
    data_version_source_projection: WorkspaceDataVersionSourceService
    projects: ProjectService
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
    reconciliation: ReconciliationService
    transformation_impacts: TransformationImpactService
    odoo_capture_publication: OdooCapturePublicationService
    odoo_capture_jobs: OdooCaptureJobManager | None
    odoo_provenance: OdooProvenanceService
    artifacts: ArtifactStore
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
