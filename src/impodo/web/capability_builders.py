"""Build cohesive local storage capabilities for browser composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..access import AuthorizationPolicy, CapabilityAuthorizationPolicy
from ..adapters.duckdb.cutover_plan_repository import CutoverPlanRepository
from ..adapters.duckdb.migration_foundation_database import (
    MigrationFoundationDatabase,
)
from ..adapters.duckdb.migration_foundation_repository import (
    MigrationFoundationRepository,
)
from ..adapters.duckdb.migration_workspace_engine_database import (
    MigrationWorkspaceEngineDatabase,
)
from ..adapters.duckdb.production_run_repository import ProductionRunRepository
from ..adapters.duckdb.recipe_repository import RecipeRepository
from ..adapters.duckdb.test_run_repository import TestRunRepository
from ..artifacts import GovernedArtifactStores, LocalArtifactStore
from ..secrets import CredentialVault, SecretStore
from ..adapters.protected_project_evidence_store import (
    ProtectedProjectEvidenceStore,
)
from ..adapters.protected_recipe_store import ProtectedRecipeStore


@dataclass(frozen=True, slots=True)
class FoundationCapability:
    """Own local registry, workspace-store, and artifact construction."""

    foundation_database: MigrationFoundationDatabase
    foundation_repository: MigrationFoundationRepository
    workspace_database: MigrationWorkspaceEngineDatabase
    artifacts: GovernedArtifactStores


def build_foundation_capability(
    project_root: str | Path,
    *,
    artifact_store: GovernedArtifactStores | None,
    lock_wait_timeout_seconds: float,
) -> FoundationCapability:
    """Build the durable local stores without opening a Project workspace."""

    foundation_database = MigrationFoundationDatabase(
        project_root,
        lock_wait_timeout_seconds=lock_wait_timeout_seconds,
    )
    return FoundationCapability(
        foundation_database=foundation_database,
        foundation_repository=MigrationFoundationRepository(foundation_database),
        workspace_database=MigrationWorkspaceEngineDatabase(
            foundation_database,
            lock_wait_timeout_seconds=lock_wait_timeout_seconds,
        ),
        artifacts=artifact_store or LocalArtifactStore(Path(project_root) / "artifacts"),
    )


@dataclass(frozen=True, slots=True)
class ProtectedRunCapability:
    """Own encrypted stores and run repositories that share those stores."""

    authorization: AuthorizationPolicy
    secret_store: SecretStore
    cutover_plan_repository: CutoverPlanRepository
    production_run_repository: ProductionRunRepository
    recipe_repository: RecipeRepository
    test_run_repository: TestRunRepository


def build_protected_run_capability(
    project_root: str | Path,
    *,
    foundation_repository: MigrationFoundationRepository,
    authorization: AuthorizationPolicy | None,
    secret_store: SecretStore | None,
) -> ProtectedRunCapability:
    """Build the authorized stores that retain protected Recipe/run evidence."""

    resolved_authorization = authorization or CapabilityAuthorizationPolicy()
    resolved_secret_store = secret_store or CredentialVault()
    return ProtectedRunCapability(
        authorization=resolved_authorization,
        secret_store=resolved_secret_store,
        cutover_plan_repository=CutoverPlanRepository(
            foundation_repository,
            ProtectedProjectEvidenceStore(project_root, resolved_secret_store),
        ),
        production_run_repository=ProductionRunRepository(foundation_repository),
        recipe_repository=RecipeRepository(
            foundation_repository,
            ProtectedRecipeStore(project_root, resolved_secret_store),
        ),
        test_run_repository=TestRunRepository(foundation_repository),
    )
