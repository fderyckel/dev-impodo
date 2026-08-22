"""Compile an eligible Authoring workspace into an optional Project Recipe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from ..access import Actor, AuthorizationPolicy, Capability
from ..domain.serialization import content_hash
from ..migration_foundation import FaultInjector, require_revision, require_uuid
from ..project_recipes import (
    ProjectRecipe,
    ProjectRecipeError,
    ProjectRecipeRepository,
    RecipePublication,
)
from .recipe_authoring_service import CompiledRecipeDefinition, RecipeDraftIssue


class WorkspaceRecipeCompiler(Protocol):
    def compile_workspace(
        self,
        workspace_id: str,
    ) -> tuple[CompiledRecipeDefinition | None, tuple[RecipeDraftIssue, ...]]: ...


@dataclass(frozen=True, slots=True)
class ProjectRecipeDraft:
    """Explain whether one workspace can currently publish reusable rules."""

    project_id: str
    data_version_id: str
    workspace_id: str
    recipe: ProjectRecipe | None
    next_recipe_revision: int
    compiled: CompiledRecipeDefinition | None
    issues: tuple[RecipeDraftIssue, ...]

    @property
    def can_publish(self) -> bool:
        return self.compiled is not None and not self.issues


class ProjectRecipePublicationService:
    """Keep Recipe publication optional and subordinate to Project ownership."""

    def __init__(
        self,
        repository: ProjectRecipeRepository,
        compiler: WorkspaceRecipeCompiler,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.compiler = compiler
        self.authorization = authorization

    def draft(
        self,
        *,
        project_id: str,
        data_version_id: str,
        workspace_id: str,
        actor: Actor,
        recipe_id: str | None = None,
    ) -> ProjectRecipeDraft:
        project_id = require_uuid(project_id, "project_id")
        data_version_id = require_uuid(data_version_id, "data_version_id")
        workspace_id = require_uuid(workspace_id, "workspace_id")
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        recipe = None
        if recipe_id is not None:
            recipe = self.repository.get_recipe(require_uuid(recipe_id, "recipe_id"))
            if recipe.project_id != project_id:
                raise ProjectRecipeError("Recipe belongs to another Project")
        compiled, issues = self.compiler.compile_workspace(workspace_id)
        return ProjectRecipeDraft(
            project_id=project_id,
            data_version_id=data_version_id,
            workspace_id=workspace_id,
            recipe=recipe,
            next_recipe_revision=(
                recipe.current_recipe_revision + 1 if recipe is not None else 1
            ),
            compiled=compiled,
            issues=issues,
        )

    def publish(
        self,
        *,
        project_id: str,
        data_version_id: str,
        workspace_id: str,
        display_name: str,
        business_purpose: str,
        actor: Actor,
        operation_id: str | None = None,
        recipe_id: str | None = None,
        expected_recipe_revision: int | None = None,
        fault: FaultInjector | None = None,
    ) -> RecipePublication:
        self.authorization.require(
            actor,
            Capability.RECIPE_PUBLISH,
            project_id=require_uuid(project_id, "project_id"),
        )
        draft = self.draft(
            project_id=project_id,
            data_version_id=data_version_id,
            workspace_id=workspace_id,
            actor=actor,
            recipe_id=recipe_id,
        )
        if not draft.can_publish or draft.compiled is None:
            if draft.issues:
                issue = draft.issues[0]
                raise ProjectRecipeError(
                    f"{issue.message} {issue.recovery_action}"
                )
            raise ProjectRecipeError("Workspace is not ready to publish a Recipe")
        if draft.recipe is not None:
            expected_recipe_revision = require_revision(
                expected_recipe_revision or 0,
                "expected_recipe_revision",
            )
        compiled = draft.compiled
        request_hash = content_hash(
            {
                "business_purpose": business_purpose.strip(),
                "data_version_id": data_version_id,
                "display_name": display_name.strip(),
                "expected_recipe_revision": expected_recipe_revision,
                "mapping_content_hash": compiled.mapping_content_hash,
                "project_id": project_id,
                "quality_ruleset_hash": compiled.quality_ruleset_hash,
                "recipe_id": recipe_id,
                "schema_hash": compiled.schema_hash,
                "semantic_hash": compiled.semantic_hash,
                "workspace_id": workspace_id,
            }
        )
        return self.repository.publish_recipe(
            project_id=project_id,
            data_version_id=data_version_id,
            workspace_id=workspace_id,
            recipe_id=recipe_id,
            expected_recipe_revision=expected_recipe_revision,
            display_name=display_name,
            business_purpose=business_purpose,
            compiled_recipe=compiled.recipe,
            compatibility_hints=compiled.compatibility_hints,
            compilation_provenance={
                "mapping_content_hash": compiled.mapping_content_hash,
                "mapping_id": compiled.mapping_id,
                "mapping_version": compiled.mapping_version,
                "quality_ruleset_hash": compiled.quality_ruleset_hash,
                "schema_hash": compiled.schema_hash,
                "source_selection_hash": compiled.source_selection_hash,
            },
            operation_id=operation_id or str(uuid4()),
            request_hash=request_hash,
            actor=actor,
            fault=fault,
        )

