"""Name the dependencies available to every local browser route.

Migration stages: cross-cutting A–J. Layer: web dependency container.

:func:`impodo.web.app.create_local_app` constructs ``WebContext`` once and
passes it to each router builder. Routes use the typed services and closed
reader callables here rather than constructing repositories or connectors.
The context contains no migration state of its own.

Target reads and the tightly scoped Stage-J writer factory remain separate
boundaries. Post-write read-back reconciliation is not in this context yet.

See ``docs/architecture/python-code-map.md`` and ``tests/test_web_app.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..access import Actor, AuthorizationPolicy
from ..application.browser_queries import BrowserQueryService
from ..application.mapping_workspace_service import MappingWorkspaceService
from ..application.normalization_service import NormalizationService
from ..application.preflight_service import PreflightService
from ..application.execution_service import ExecutionService
from ..application.preparation_service import PreparationService
from ..application.quality_service import QualityService
from ..application.resolution_service import ResolutionService
from ..application.schema_workspace_service import SchemaWorkspaceService
from ..application.source_workspace_service import SourceWorkspaceService
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
from ..jobs import JobDispatcher
from ..local_odoo_reader import (
    LocalOdooMetadataReader,
)
from ..local_stack import LocalStackService
from ..odoo_writer import OdooWriteExecutor
from ..projects import MigrationProject, ProjectService
from ..secrets import SecretStore

ConnectionTester = Callable[[MigrationProject, str], str]

SchemaReader = Callable[[MigrationProject, str], MetadataSnapshot]

ModelCatalogReader = Callable[[MigrationProject, str], RecordSnapshot]

BrowserReadinessReader = Callable[
    [
        MigrationProject,
        tuple[MetadataRequest, ...],
        tuple[RecordRequest, ...],
    ],
    tuple[MetadataSnapshot, RecordSnapshot],
]

OdooWriteExecutorFactory = Callable[[MigrationProject, str], OdooWriteExecutor]


@dataclass(slots=True)
class WebContext:
    """Share one assembled set of local services and boundary callables.

    Service fields expose browser use cases; ``queries`` provides read-only
    projections; reader callables isolate target I/O; and actor,
    authorization, artifacts, jobs, secrets, and local-stack services provide
    cross-cutting boundaries. The object is mutable only so the local launcher
    and tests can replace explicitly injectable runtime seams.
    """

    queries: BrowserQueryService
    projects: ProjectService
    intake: SourceIntakeService
    inspections: SourceInspectionService
    sources: SourceWorkspaceService
    derived_entities: DerivedEntityWorkspaceService
    schema_workspace: SchemaWorkspaceService
    mapping_workspace: MappingWorkspaceService
    preparation: PreparationService
    quality: QualityService
    resolution: ResolutionService
    normalization: NormalizationService
    preflight: PreflightService
    execution: ExecutionService
    transformation_impacts: TransformationImpactService
    artifacts: ArtifactStore
    actor: Actor
    authorization: AuthorizationPolicy
    jobs: JobDispatcher
    secret_store: SecretStore
    launch_token: str
    connection_tester: ConnectionTester
    schema_reader: SchemaReader
    model_catalog_reader: ModelCatalogReader
    readiness_reader: BrowserReadinessReader | None
    write_executor_factory: OdooWriteExecutorFactory
    local_stack: LocalStackService
    local_odoo_reader: LocalOdooMetadataReader
