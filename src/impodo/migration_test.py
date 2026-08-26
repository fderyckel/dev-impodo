"""Define the fresh setup phase for one Project-owned Test run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .access import ActorIdentity
from .domain.data_version.models import DataVersion
from .domain.serialization import content_hash
from .migration_foundation import (
    require_aware,
    require_revision,
    require_uuid,
    required_text,
)
from .migration_run_planning import RecipeDependency, RecipeRevisionSelection
from .domain.run.models import MigrationRun
from .domain.workspace.models import MigrationWorkspace


class TestRunSetupState(StrEnum):
    SETUP = "SETUP"
    ACTIVE = "ACTIVE"


@dataclass(frozen=True, slots=True)
class TestRunSetupBinding:
    """Pin selected Recipe versions while fresh Test evidence is prepared."""

    test_run_setup_id: str
    project_id: str
    migration_run_id: str
    data_version_id: str
    setup_workspace_id: str
    selected_revisions: tuple[RecipeRevisionSelection, ...]
    dependencies: tuple[RecipeDependency, ...]
    state: TestRunSetupState
    target_binding_id: str | None
    created_at: datetime
    activated_at: datetime | None = None
    contract_version: int = 1

    def __post_init__(self) -> None:
        for value, name in (
            (self.test_run_setup_id, "test_run_setup_id"),
            (self.project_id, "project_id"),
            (self.migration_run_id, "migration_run_id"),
            (self.data_version_id, "data_version_id"),
            (self.setup_workspace_id, "setup_workspace_id"),
        ):
            require_uuid(value, name)
        if self.target_binding_id is not None:
            require_uuid(self.target_binding_id, "target_binding_id")
        if self.contract_version != 1:
            raise ValueError("Test run setup contract is unsupported")
        selections = tuple(sorted(self.selected_revisions))
        if not selections:
            raise ValueError("Select at least one Recipe version")
        recipe_ids = tuple(item.recipe_id for item in selections)
        if len(set(recipe_ids)) != len(recipe_ids):
            raise ValueError("Select each Recipe only once")
        object.__setattr__(self, "selected_revisions", selections)
        dependencies = tuple(
            sorted(
                self.dependencies,
                key=lambda item: (item.before_recipe_id, item.after_recipe_id),
            )
        )
        if any(
            item.before_recipe_id not in recipe_ids
            or item.after_recipe_id not in recipe_ids
            for item in dependencies
        ):
            raise ValueError("Recipe order must use selected Recipes")
        object.__setattr__(self, "dependencies", dependencies)
        object.__setattr__(self, "state", TestRunSetupState(self.state))
        require_aware(self.created_at, "created_at")
        if self.activated_at is not None:
            require_aware(self.activated_at, "activated_at")
        if self.state is TestRunSetupState.SETUP and (
            self.target_binding_id is not None or self.activated_at is not None
        ):
            raise ValueError("A Test setup cannot contain activation evidence")
        if self.state is TestRunSetupState.ACTIVE and (
            self.target_binding_id is None or self.activated_at is None
        ):
            raise ValueError("An active Test run requires activation evidence")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "activated_at": self.activated_at.isoformat()
            if self.activated_at
            else None,
            "contract_version": self.contract_version,
            "created_at": self.created_at.isoformat(),
            "data_version_id": self.data_version_id,
            "dependencies": [item.to_dict() for item in self.dependencies],
            "migration_run_id": self.migration_run_id,
            "project_id": self.project_id,
            "selected_revisions": [item.to_dict() for item in self.selected_revisions],
            "setup_workspace_id": self.setup_workspace_id,
            "state": self.state.value,
            "target_binding_id": self.target_binding_id,
            "test_run_setup_id": self.test_run_setup_id,
        }
        if include_hash:
            value["content_hash"] = self.content_hash
        return value

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> TestRunSetupBinding:
        selections = tuple(
            RecipeRevisionSelection(
                recipe_id=str(item["recipe_id"]),
                recipe_revision=int(item["recipe_revision"]),
                semantic_hash=str(item["semantic_hash"]),
            )
            for item in value["selected_revisions"]  # type: ignore[union-attr]
        )
        dependencies = tuple(
            RecipeDependency(
                before_recipe_id=str(item["before_recipe_id"]),
                after_recipe_id=str(item["after_recipe_id"]),
                kind=str(item.get("kind", "PROJECT_SEQUENCE")),
                reason=str(
                    item.get(
                        "reason",
                        "The data manager selected this integrated run order.",
                    )
                ),
            )
            for item in value["dependencies"]  # type: ignore[union-attr]
        )
        result = cls(
            test_run_setup_id=str(value["test_run_setup_id"]),
            project_id=str(value["project_id"]),
            migration_run_id=str(value["migration_run_id"]),
            data_version_id=str(value["data_version_id"]),
            setup_workspace_id=str(value["setup_workspace_id"]),
            selected_revisions=selections,
            dependencies=dependencies,
            state=str(value["state"]),
            target_binding_id=(
                str(value["target_binding_id"])
                if value.get("target_binding_id")
                else None
            ),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            activated_at=(
                datetime.fromisoformat(str(value["activated_at"]))
                if value.get("activated_at")
                else None
            ),
            contract_version=int(value["contract_version"]),
        )
        claimed = value.get("content_hash")
        if claimed is not None and claimed != result.content_hash:
            raise ValueError("Stored Test run setup hash is inconsistent")
        return result


@dataclass(frozen=True, slots=True)
class RecipeRunParameterValue:
    """Hold one normalized answer for one exact Recipe application."""

    recipe_id: str
    logical_parameter_id: str
    value: str | int

    def __post_init__(self) -> None:
        require_uuid(self.recipe_id, "recipe_id")
        object.__setattr__(
            self,
            "logical_parameter_id",
            required_text(
                self.logical_parameter_id,
                "logical_parameter_id",
                maximum=120,
            ),
        )
        if not self.logical_parameter_id.startswith("parameter:"):
            raise ValueError("Run value must use a Recipe parameter identity")
        if isinstance(self.value, bool) or not isinstance(self.value, (str, int)):
            raise TypeError("Run value has an unsupported stored type")

    def to_dict(self) -> dict[str, object]:
        return {
            "logical_parameter_id": self.logical_parameter_id,
            "recipe_id": self.recipe_id,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class TestRunParameterValues:
    """Record the run-owned answers collected from selected Recipes."""

    test_run_setup_id: str
    project_id: str
    migration_run_id: str
    revision: int
    values: tuple[RecipeRunParameterValue, ...]
    updated_by: ActorIdentity
    updated_at: datetime
    contract_version: int = 1

    def __post_init__(self) -> None:
        for value, name in (
            (self.test_run_setup_id, "test_run_setup_id"),
            (self.project_id, "project_id"),
            (self.migration_run_id, "migration_run_id"),
        ):
            require_uuid(value, name)
        require_revision(self.revision, "run_parameter_values_revision")
        if self.contract_version != 1:
            raise ValueError("Test run value contract is unsupported")
        ordered = tuple(
            sorted(
                self.values,
                key=lambda item: (item.recipe_id, item.logical_parameter_id),
            )
        )
        identities = tuple(
            (item.recipe_id, item.logical_parameter_id) for item in ordered
        )
        if len(set(identities)) != len(identities):
            raise ValueError("Store each Recipe run value only once")
        object.__setattr__(self, "values", ordered)
        require_aware(self.updated_at, "updated_at")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    @property
    def by_recipe(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for item in self.values:
            result.setdefault(item.recipe_id, {})[
                item.logical_parameter_id
            ] = item.value
        return result

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "contract_version": self.contract_version,
            "migration_run_id": self.migration_run_id,
            "project_id": self.project_id,
            "revision": self.revision,
            "test_run_setup_id": self.test_run_setup_id,
            "updated_at": self.updated_at.isoformat(),
            "updated_by": {
                "display_name": self.updated_by.display_name,
                "issuer": self.updated_by.issuer,
                "subject_id": self.updated_by.subject_id,
            },
            "values": [item.to_dict() for item in self.values],
        }
        if include_hash:
            value["content_hash"] = self.content_hash
        return value


@dataclass(frozen=True, slots=True)
class TestRunSetupBundle:
    data_version: DataVersion
    run: MigrationRun
    setup_workspace: MigrationWorkspace
    binding: TestRunSetupBinding
