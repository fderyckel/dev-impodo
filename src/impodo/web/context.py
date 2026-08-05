"""Typed dependencies assembled by the local FastAPI composition root."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from ..access import Actor, AuthorizationPolicy
from ..artifacts import ArtifactStore
from ..connectors import MetadataRequest, MetadataSnapshot, RecordRequest, RecordSnapshot
from ..derived_entities import DerivedEntityWorkspaceService
from ..intake import SourceIntakeService
from ..inspection import SourceInspectionService
from ..jobs import JobDispatcher
from ..local_odoo_reader import (
    LocalOdooMetadataReader,
)
from ..local_stack import LocalStackService
from ..projects import MigrationProject, ProjectService
from ..secrets import SecretStore
from ..workspace import MappingWorkspaceService, SchemaWorkspaceService, SourceWorkspaceService
from ..application.browser_queries import BrowserQueryService
from ..application.normalization_service import NormalizationService
from ..application.preflight_service import PreflightService
from ..application.preparation_service import PreparationService
from ..application.quality_service import QualityService
from ..application.transformation_impact_service import TransformationImpactService
from ..application.readiness_workflow_service import ReadinessWorkflowService

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

@dataclass(slots=True)
class WebContext:
    queries: BrowserQueryService
    projects: ProjectService
    intake: SourceIntakeService
    inspections: SourceInspectionService
    sources: SourceWorkspaceService
    derived_entities: DerivedEntityWorkspaceService
    schema_workspace: SchemaWorkspaceService
    mapping_workspace: MappingWorkspaceService
    preparation: PreparationService
    readiness_workflow: ReadinessWorkflowService
    quality: QualityService
    normalization: NormalizationService
    preflight: PreflightService
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
    local_stack: LocalStackService
    local_odoo_reader: LocalOdooMetadataReader
