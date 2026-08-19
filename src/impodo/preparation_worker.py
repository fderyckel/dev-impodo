"""Project-scoped composition root for spawned preparation workers."""

from __future__ import annotations

from pathlib import Path

from .access import CapabilityAuthorizationPolicy
from .adapters.duckdb.advanced_coverage_repository import (
    AdvancedCoverageRepository,
)
from .adapters.duckdb.database import DuckDbProjectDatabase
from .adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from .adapters.duckdb.mapping_repository import MappingRepository
from .adapters.duckdb.normalization_repository import NormalizationRepository
from .adapters.duckdb.odoo_provenance_repository import OdooProvenanceRepository
from .adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from .adapters.duckdb.project_workspace_reader import ProjectWorkspaceReader
from .adapters.duckdb.quality_repository import QualityRepository
from .adapters.duckdb.recipe_application_repository import (
    RecipeApplicationRepository,
)
from .adapters.duckdb.source_repository import SourceRepository
from .adapters.duckdb.staging_repository import StagingRepository
from .application.normalization_service import NormalizationService
from .application.odoo_provenance_service import OdooProvenanceService
from .application.preparation_service import PreparationService
from .application.quality_service import QualityService
from .application.resolution_service import ResolutionService
from .artifacts import LocalArtifactStore
from .preparation_jobs import PreparationWorkspace
from .secrets import CredentialVault


PREPARATION_DATABASE_HANDOFF_TIMEOUT_SECONDS = 5.0


def create_preparation_worker(
    project_root: str | Path,
    *,
    workspace: PreparationWorkspace,
) -> PreparationService:
    """Compose preparation from one project database and no shared registry.

    Recipe/DataVersion authorization is resolved by the browser process before
    spawn and captured in ``workspace``. The worker validates that exact
    identity against the immutable linkage inside the project database.
    """

    database = DuckDbProjectDatabase(
        project_root,
        lock_wait_timeout_seconds=PREPARATION_DATABASE_HANDOFF_TIMEOUT_SECONDS,
    )
    artifacts = LocalArtifactStore(project_root)
    authorization = CapabilityAuthorizationPolicy()
    secrets = CredentialVault()
    projects = ProjectWorkspaceReader(database, workspace)
    derived_entities = DerivedEntityRepository(database)
    sources = SourceRepository(database, derived_entities)
    mappings = MappingRepository(database)
    staging = StagingRepository(database, artifacts)
    sessions = PreparationSessionRepository(database, artifacts)
    coverage = AdvancedCoverageRepository(database)
    quality_repository = QualityRepository(database, projects)
    normalization_repository = NormalizationRepository(database, projects)
    recipe_quality = RecipeApplicationRepository(database)
    quality = QualityService(
        mappings,
        sources,
        quality_repository,
        recipe_quality=recipe_quality,
    )
    normalization = NormalizationService(
        normalization_repository,
        authorization,
    )
    resolution = ResolutionService(coverage, staging)
    odoo_provenance_repository = OdooProvenanceRepository(database, artifacts)
    odoo_provenance = OdooProvenanceService(
        projects,
        sources,
        odoo_provenance_repository,
        secrets,
        authorization,
    )
    return PreparationService(
        projects,
        sources,
        derived_entities,
        mappings,
        staging,
        sessions,
        artifacts,
        authorization,
        quality,
        normalization,
        resolution,
        odoo_provenance=odoo_provenance,
    )
