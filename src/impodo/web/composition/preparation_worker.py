"""Workspace-scoped composition root for spawned preparation workers."""

from __future__ import annotations

from pathlib import Path

from impodo.domain.shared.access import CapabilityAuthorizationPolicy
from impodo.adapters.duckdb.advanced_coverage_repository import (
    AdvancedCoverageRepository,
)
from impodo.adapters.duckdb.derived_entity_repository import DerivedEntityRepository
from impodo.adapters.duckdb.mapping_repository import MappingRepository
from impodo.adapters.duckdb.normalization_repository import NormalizationRepository
from impodo.adapters.duckdb.odoo_provenance_repository import OdooProvenanceRepository
from impodo.adapters.duckdb.preparation_session_repository import (
    PreparationSessionRepository,
)
from impodo.adapters.duckdb.workspace_state_reader import WorkspaceStateReader
from impodo.adapters.duckdb.quality_repository import QualityRepository
from impodo.adapters.duckdb.migration_workspace_engine_database import (
    FixedMigrationWorkspaceEngineDatabase,
)
from impodo.adapters.duckdb.source_repository import SourceRepository
from impodo.adapters.duckdb.staging_repository import StagingRepository
from impodo.adapters.polars_transformation import PolarsTransformationAdapter
from impodo.adapters.protected_odoo_comparison import ProtectedOdooComparisonCodec
from impodo.adapters.protected_odoo_provenance import ProtectedOdooProvenanceCodec
from impodo.application.workspace.preparation.normalization_service import NormalizationService
from impodo.application.odoo_provenance_service import OdooProvenanceService
from impodo.application.workspace.preparation.preparation_service import PreparationService
from impodo.application.workspace.preparation.quality_service import QualityService
from impodo.application.workspace.preparation.resolution_service import ResolutionService
from impodo.adapters.artifacts.local_store import LocalArtifactStore
from impodo.application.workspace.preparation.job_models import PreparationWorkspace
from impodo.adapters.protected_evidence.credential_vault import CredentialVault
from impodo.application.workspace.access import WorkspaceAccessService


PREPARATION_DATABASE_HANDOFF_TIMEOUT_SECONDS = 5.0


def create_preparation_worker(
    project_root: str | Path,
    *,
    workspace: PreparationWorkspace,
) -> PreparationService:
    """Compose preparation from one workspace database and no shared registry.

    Project/DataVersion authorization is resolved by the browser process before
    spawn and captured in ``workspace``. The worker validates those identities
    against the isolated workspace and DataVersion stores.
    """

    database = FixedMigrationWorkspaceEngineDatabase(
        project_root,
        project_id=workspace.project_id,
        workspace_id=workspace.workspace_id,
        data_version_id=workspace.data_version_id,
        migration_run_id=workspace.migration_run_id,
        recipe_application_id=workspace.recipe_application_id,
        lock_wait_timeout_seconds=PREPARATION_DATABASE_HANDOFF_TIMEOUT_SECONDS,
    )
    artifacts = LocalArtifactStore(Path(project_root) / "artifacts")
    authorization = WorkspaceAccessService(
        database,
        CapabilityAuthorizationPolicy(),
    )
    secrets = CredentialVault()
    workspace_states = WorkspaceStateReader(database, workspace)
    derived_entities = DerivedEntityRepository(database)
    sources = SourceRepository(database, derived_entities)
    mappings = MappingRepository(database)
    staging = StagingRepository(database, artifacts)
    sessions = PreparationSessionRepository(database, artifacts)
    coverage = AdvancedCoverageRepository(database)
    quality_repository = QualityRepository(database, workspace_states)
    normalization_repository = NormalizationRepository(database, workspace_states)
    quality = QualityService(
        mappings,
        sources,
        quality_repository,
    )
    normalization = NormalizationService(
        normalization_repository,
        authorization,
    )
    resolution = ResolutionService(coverage, staging)
    odoo_provenance_repository = OdooProvenanceRepository(
        database,
        artifacts,
        protected_root=lambda workspace_id: (
            Path(project_root)
            / "artifacts"
            / "dv"
            / database.resolve_workspace_access_context(workspace_id).data_version_id
            / "protected"
        ),
    )
    odoo_provenance = OdooProvenanceService(
        workspace_states,
        sources,
        odoo_provenance_repository,
        secrets,
        authorization,
        ProtectedOdooProvenanceCodec(),
        ProtectedOdooComparisonCodec(),
    )
    return PreparationService(
        workspace_states,
        sources,
        derived_entities,
        mappings,
        staging,
        sessions,
        artifacts,
        authorization,
        quality,
        normalization,
        PolarsTransformationAdapter(),
        resolution,
        odoo_provenance=odoo_provenance,
    )
