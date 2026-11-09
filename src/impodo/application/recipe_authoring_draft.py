"""Provision a separate Authoring workspace for one immutable Recipe version."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from threading import RLock
from uuid import UUID, uuid5

from impodo.domain.coverage import CoverageScopeRevision
from impodo.domain.data_version.models import DataVersionPurpose, DataVersionState
from impodo.domain.project.foundation import (
    MigrationNotFoundError,
    MigrationOperationKind,
    MigrationOperationState,
)
from impodo.domain.recipe.models import RecipeError, RecipeRevisionRead
from impodo.domain.run.models import MigrationRunPurpose
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor, Capability
from impodo.domain.workspace.contracts import OdooSchemaCatalog, SourceSelection
from impodo.domain.workspace.models import MigrationWorkspace, MigrationWorkspaceState
from impodo.domain.workspace.workbench import WorkspaceState, WorkspaceStatus

from .data_version.source_packages import (
    SourcePackageOrigin,
    SourcePackageState,
    WorkspaceSourceProjectionService,
)
from .migration_project_authoring_service import MigrationProjectAuthoringService
from .recipe_application_service import RecipeApplicationService
from .workspace.views import WorkspaceOwnerViewService


@dataclass(frozen=True, slots=True)
class RecipeCoverageReview:
    """The saved decisions and a token for the new workspace's approval."""

    scope: CoverageScopeRevision
    checks: tuple[dict[str, object], ...]
    binding_hash: str


class RecipeAuthoringDraftService:
    """Reuse the accepted delivery while leaving target evidence to the new run."""

    def __init__(
        self,
        authoring: MigrationProjectAuthoringService,
        projections: WorkspaceSourceProjectionService,
        compiler: RecipeApplicationService,
        workspace_views: WorkspaceOwnerViewService,
    ) -> None:
        self.authoring = authoring
        self.projections = projections
        self.compiler = compiler
        self.workspace_views = workspace_views
        self._creation_lock = RLock()

    @staticmethod
    def _operation(saved: RecipeRevisionRead, purpose: str) -> str:
        return str(
            uuid5(
                UUID(saved.recipe.recipe_id),
                f"recipe-authoring:{saved.revision.version}:{purpose}",
            )
        )

    @staticmethod
    def _seed_operation(workspace_id: str, target_hash: str) -> str:
        return str(uuid5(UUID(workspace_id), f"seed-saved-recipe:{target_hash}"))

    @staticmethod
    def _provenance(saved: RecipeRevisionRead, key: str) -> str:
        provenance = saved.envelope.get("provenance")
        value = provenance.get(key) if isinstance(provenance, Mapping) else None
        if not isinstance(value, str) or not value:
            raise RecipeError("This version has no accepted Authoring delivery.")
        return value

    @staticmethod
    def definition(saved: RecipeRevisionRead) -> Mapping[str, object]:
        """Use the authenticated Recipe's reusable rules."""

        value = saved.envelope.get("recipe")
        if not isinstance(value, Mapping):
            raise RecipeError("This version's saved rules are unavailable.")
        return value

    def _saved_coverage(self, saved: RecipeRevisionRead, *, actor: Actor) -> CoverageScopeRevision:
        origin_id = self._provenance(saved, "origin_workspace_id")
        self.compiler.schema_workspace.authorization.require(
            actor, Capability.PROJECT_VIEW, workspace_id=origin_id,
        )
        scope = self.compiler.references.get_saved_coverage_scope(
            origin_id, self._provenance(saved, "quality_ruleset_hash"),
        )
        if scope is None:
            raise RecipeError("The approved coverage scope for this Recipe version is unavailable.")
        return scope

    def coverage_review(
        self, saved: RecipeRevisionRead, schema: OdooSchemaCatalog, *, actor: Actor,
    ) -> RecipeCoverageReview | None:
        """Project one historical scope against the new Odoo capture."""
        checks = self.compiler.advanced_checks(self.definition(saved))
        if not checks:
            return None
        scope = self._saved_coverage(saved, actor=actor)
        binding = content_hash((
            saved.recipe.recipe_id, saved.revision.version, saved.revision.semantic_hash,
            self._provenance(saved, "origin_data_version_id"), scope.content_hash,
            schema.workspace_id, schema.content_hash, schema.connection_target_hash,
            schema.read_credential_binding_hash, schema.read_context_hash,
            schema.read_principal_hash, schema.read_permission_hash,
        ))
        return RecipeCoverageReview(scope, checks, binding)

    def find(
        self, saved: RecipeRevisionRead, *, actor: Actor
    ) -> MigrationWorkspace | None:
        """Read one completed source-projection intent by its exact identity."""

        self.authoring.projects.authorization.require(
            actor, Capability.RECIPE_PUBLISH, project_id=saved.recipe.project_id
        )
        try:
            intent = (
                self.authoring.migration_workspaces.repository.get_operation_intent(
                    self._operation(saved, "source")
                )
            )
        except MigrationNotFoundError:
            return None
        if (
            intent.project_id != saved.recipe.project_id
            or intent.kind is not MigrationOperationKind.WORKSPACE_SOURCE_PROJECT
        ):
            raise RecipeError("The Recipe draft has an invalid workspace binding.")
        if intent.state is not MigrationOperationState.COMMITTED:
            return None
        owner = self.workspace_views.get(intent.owner_id, actor=actor)
        workspace = owner.migration_workspace
        if (
            workspace.project_id != saved.recipe.project_id
            or workspace.data_version_id
            != self._provenance(saved, "origin_data_version_id")
            or workspace.recipe_application_id is not None
            or owner.migration_run.purpose is not MigrationRunPurpose.AUTHORING
        ):
            raise RecipeError("The Recipe draft has an invalid source binding.")
        return workspace

    def create(self, saved: RecipeRevisionRead, *, actor: Actor) -> MigrationWorkspace:
        """Create or resume the single draft for this version and delivery."""

        with self._creation_lock:
            return self._create(saved, actor=actor)

    def _create(self, saved: RecipeRevisionRead, *, actor: Actor) -> MigrationWorkspace:
        project_id = saved.recipe.project_id
        for capability in (
            Capability.RECIPE_PUBLISH,
            Capability.MIGRATION_RUN_CREATE,
            Capability.MIGRATION_WORKSPACE_CREATE,
            Capability.MIGRATION_WORKSPACE_EDIT,
            Capability.PROJECT_EDIT,
            Capability.PROJECT_REGISTER,
            Capability.MIGRATION_RUN_EDIT,
            Capability.SCHEMA_DISCOVER,
            Capability.SCHEMA_GOVERN,
            Capability.MAPPING_EDIT,
        ):
            self.authoring.projects.authorization.require(
                actor, capability, project_id=project_id
            )
        current = self.find(saved, actor=actor)
        if current is not None:
            if current.state is MigrationWorkspaceState.CLOSED:
                raise RecipeError("The Authoring draft for this version is closed.")
            return current
        definition = self.definition(saved)
        if self.compiler.reference_requirements(definition):
            self.authoring.projects.authorization.require(
                actor, Capability.COVERAGE_SCOPE, project_id=project_id,
            )
        delivery_id = self._provenance(saved, "origin_data_version_id")
        delivery = self.authoring.data_versions.get(delivery_id, actor=actor)
        if (
            delivery.project_id != project_id
            or delivery.purpose is not DataVersionPurpose.AUTHORING
            or delivery.state is not DataVersionState.FROZEN
        ):
            raise RecipeError("This version has no accepted Authoring delivery.")
        package = self.authoring.source_packages.repository.get_source_package(
            delivery_id
        )
        if (
            package is None
            or package.project_id != project_id
            or package.origin is not SourcePackageOrigin.FILE
            or package.state is not SourcePackageState.FROZEN
            or package.content_hash != delivery.source_package_hash
        ):
            raise RecipeError("The accepted file delivery is unavailable.")
        selection = SourceSelection(
            selection_id=delivery_id,
            version=1,
            data_version_id=delivery_id,
            created_at=delivery.created_at,
            created_by=actor.identity.display_name,
            datasets=tuple(item.to_mapping_dataset() for item in package.datasets),
            content_hash=package.content_hash,
        )
        bindings = self.compiler.authoring_source_bindings(definition, selection)
        dataset_ids = {value for key, value in bindings.items() if key.startswith("dataset:")}
        if not dataset_ids:
            raise RecipeError("This version has no reusable source tables.")
        origin = self.workspace_views.get(
            self._provenance(saved, "origin_workspace_id"), actor=actor
        )
        if origin.project_id != project_id or origin.data_version_id != delivery_id:
            raise RecipeError(
                "This version has an invalid Authoring workspace binding."
            )
        if self.compiler.advanced_checks(definition):
            scope = self._saved_coverage(saved, actor=actor)
            scoped_names = {name for item in scope.declarations for name in item.datasets}
            dataset_ids.update(item.dataset_id for item in selection.datasets if item.name in scoped_names)
        prior_target = origin.target_setup
        project = self.authoring.projects.get(project_id, actor=actor)
        workspace, state = self.authoring.open_revision_workspace(
            project,
            delivery,
            label=f"{saved.recipe.display_name[:170]} · Recipe v{saved.revision.version} draft",
            run_operation=self._operation(saved, "run"),
            workspace_operation=self._operation(saved, "workspace"),
            actor=actor,
        )
        models = tuple(item.model for item in self.compiler.requirements(definition))
        if prior_target is not None and state.odoo_connection_mode is None:
            state = self.authoring.workspace_states.update_target(
                workspace.workspace_id,
                actor=actor,
                expected_revision=state.revision,
                odoo_connection_mode=prior_target.connection_mode.value,
                odoo_base_url=prior_target.base_url,
                odoo_database=prior_target.database,
                intended_applications=prior_target.intended_applications,
                intended_models=models,
            )
        if state.status is WorkspaceStatus.DRAFT:
            self.authoring.workspace_states.register(
                workspace.workspace_id, actor=actor, expected_revision=state.revision
            )
            workspace = self.authoring.migration_workspaces.get(
                workspace.workspace_id, actor=actor
            )
        operation_id = self._operation(saved, "source")
        try:
            pending = (
                self.authoring.migration_workspaces.repository.get_operation_intent(
                    operation_id
                )
            )
        except MigrationNotFoundError:
            expected_revision = workspace.optimistic_revision
        else:
            expected_revision = (
                pending.expected_revision or workspace.optimistic_revision
            )
        self.projections.materialize(
            workspace.workspace_id,
            actor=actor,
            dataset_ids=tuple(sorted(dataset_ids)),
            expected_workspace_revision=expected_revision,
            operation_id=operation_id,
        )
        return workspace

    def seed(
        self,
        saved: RecipeRevisionRead,
        workspace: MigrationWorkspace,
        schema: OdooSchemaCatalog,
        state: WorkspaceState,
        *,
        actor: Actor,
        coverage_review_hash: str = "",
    ) -> None:
        """Apply the exact Recipe to the fresh target without submitting matches."""

        if schema.pending_refresh is not None:
            raise RecipeError(
                "Review the Odoo field changes before opening Match data."
            )
        definition = self.definition(saved)
        operation_id = self._seed_operation(workspace.workspace_id, schema.connection_target_hash)
        scope = None
        if self.compiler.advanced_checks(definition) and not self.compiler.has_authoring_seed(
            workspace.workspace_id, operation_id,
        ):
            if not coverage_review_hash:
                raise RecipeError("Review and approve the saved advanced checks.")
            review = self.coverage_review(saved, schema, actor=actor)
            if review is None or coverage_review_hash != review.binding_hash:
                raise RecipeError("Review and approve the saved advanced checks.")
            self.compiler.schema_workspace.authorization.require(
                actor, Capability.COVERAGE_SCOPE, workspace_id=workspace.workspace_id,
            )
            scope = review.scope
        self.compiler.seed_authoring_mapping(
            workspace.workspace_id,
            definition=definition,
            schema=schema,
            state=state,
            operation_id=operation_id,
            recipe_hash=saved.revision.semantic_hash,
            origin_workspace_id=self._provenance(saved, "origin_workspace_id"),
            coverage_scope=scope,
            actor=actor,
        )
