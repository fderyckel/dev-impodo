"""Authorize Project-scoped Recipe reads and publication coordination."""

from __future__ import annotations

from typing import Mapping

from ...access import Actor, AuthorizationPolicy, Capability
from ...domain.recipe.models import (
    Recipe,
    RecipeError,
    RecipeRevision,
    RecipeRevisionRead,
)
from ...migration_foundation import require_revision, require_uuid
from .ports import RecipeRepository


class RecipeService:
    """Authorize Project-scoped Recipe reads and publication coordination."""

    def __init__(
        self,
        repository: RecipeRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def get(self, recipe_id: str, *, actor: Actor) -> Recipe:
        self.authorization.require(actor, Capability.RECIPE_VIEW)
        recipe = self.repository.get_recipe(require_uuid(recipe_id, "recipe_id"))
        self.authorization.require(
            actor,
            Capability.RECIPE_VIEW,
            project_id=recipe.project_id,
        )
        return recipe

    def list(
        self,
        project_id: str,
        *,
        actor: Actor,
    ) -> tuple[Recipe, ...]:
        project_id = require_uuid(project_id, "project_id")
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        return self.repository.list_recipes(project_id)

    def revisions(
        self,
        recipe_id: str,
        *,
        actor: Actor,
    ) -> tuple[RecipeRevision, ...]:
        self.get(recipe_id, actor=actor)
        return self.repository.list_recipe_revisions(recipe_id)

    def read_revision(
        self,
        recipe_id: str,
        version: int,
        *,
        actor: Actor,
    ) -> Mapping[str, object]:
        self.get(recipe_id, actor=actor)
        return self.repository.read_recipe_revision(
            recipe_id,
            require_revision(version, "version"),
        )

    def read_revisions(
        self,
        project_id: str,
        selections: tuple[tuple[str, int], ...],
        *,
        actor: Actor,
    ) -> Mapping[tuple[str, int], RecipeRevisionRead]:
        """Read exact Project-owned revisions with one bounded registry query."""

        project_id = require_uuid(project_id, "project_id")
        normalized = tuple(
            (
                require_uuid(recipe_id, "recipe_id"),
                require_revision(version, "version"),
            )
            for recipe_id, version in selections
        )
        if len(set(normalized)) != len(normalized):
            raise RecipeError("Select each Recipe version only once")
        self.authorization.require(actor, Capability.RECIPE_VIEW)
        self.authorization.require(
            actor,
            Capability.RECIPE_VIEW,
            project_id=project_id,
        )
        return self.repository.read_recipe_revisions(project_id, normalized)
