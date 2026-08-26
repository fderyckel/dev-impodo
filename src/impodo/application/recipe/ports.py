"""Declare the persistence port consumed by Recipe application commands."""

from __future__ import annotations

from typing import Mapping, Protocol

from ...access import Actor
from ...domain.recipe.models import (
    Recipe,
    RecipePublication,
    RecipeRevision,
    RecipeRevisionRead,
)
from ...migration_foundation import FaultInjector


class RecipeRepository(Protocol):
    """Persist Recipes without exposing the registry or protected store."""

    def get_recipe(self, recipe_id: str) -> Recipe: ...

    def list_recipes(self, project_id: str) -> tuple[Recipe, ...]: ...

    def list_recipe_revisions(
        self,
        recipe_id: str,
    ) -> tuple[RecipeRevision, ...]: ...

    def read_recipe_revision(
        self,
        recipe_id: str,
        version: int,
    ) -> Mapping[str, object]: ...

    def read_recipe_revisions(
        self,
        project_id: str,
        selections: tuple[tuple[str, int], ...],
    ) -> Mapping[tuple[str, int], RecipeRevisionRead]: ...

    def publish_recipe(
        self,
        *,
        project_id: str,
        data_version_id: str,
        workspace_id: str,
        recipe_id: str | None,
        expected_recipe_revision: int | None,
        display_name: str,
        business_purpose: str,
        compiled_recipe: Mapping[str, object],
        compatibility_hints: Mapping[str, object],
        compilation_provenance: Mapping[str, object],
        operation_id: str,
        request_hash: str,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> RecipePublication: ...
