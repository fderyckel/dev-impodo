"""Bind saved Production answers to their exact setup and qualified plan."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from impodo.domain.project.foundation import MigrationConflictError, require_aware, require_hash, require_revision, require_uuid
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import ActorIdentity


@dataclass(frozen=True, slots=True)
class ProductionRunValues:
    production_run_binding_id: str
    project_id: str
    migration_run_id: str
    plan_content_hash: str
    revision: int
    parameters: Mapping[str, Mapping[str, object]]
    controls: Mapping[str, Mapping[str, str]]
    updated_by: ActorIdentity
    updated_at: datetime

    def __post_init__(self) -> None:
        for name in ("production_run_binding_id", "project_id", "migration_run_id"):
            require_uuid(getattr(self, name), name)
        require_hash(self.plan_content_hash, "plan_content_hash")
        require_revision(self.revision, "revision")
        require_aware(self.updated_at, "updated_at")
        for name in ("parameters", "controls"):
            values = getattr(self, name)
            for recipe_id in values:
                require_uuid(recipe_id, "recipe_id")
            object.__setattr__(self, name, MappingProxyType({
                recipe_id: MappingProxyType(dict(answers)) for recipe_id, answers in values.items()
            }))

    @property
    def by_recipe(self) -> dict[str, dict[str, object]]:
        return {key: dict(value) for key, value in self.parameters.items()}

    @property
    def controls_by_recipe(self) -> dict[str, dict[str, str]]:
        return {key: dict(value) for key, value in self.controls.items()}

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": 1,
            "production_run_binding_id": self.production_run_binding_id,
            "project_id": self.project_id, "migration_run_id": self.migration_run_id,
            "plan_content_hash": self.plan_content_hash, "revision": self.revision,
            "parameters": self.by_recipe, "controls": self.controls_by_recipe,
            "updated_by": {"issuer": self.updated_by.issuer, "subject_id": self.updated_by.subject_id,
                           "display_name": self.updated_by.display_name},
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ProductionRunValues":
        values = dict(payload)
        if values.pop("contract_version") != 1:
            raise MigrationConflictError("Saved Production values have an unsupported version")
        values["updated_by"] = ActorIdentity(**values["updated_by"])
        values["updated_at"] = datetime.fromisoformat(values["updated_at"])
        return cls(**values)
