"""Bind saved Production answers to their exact setup and qualified plan."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from impodo.domain.project.foundation import (
    MigrationConflictError,
    require_aware,
    require_hash,
    require_revision,
    require_uuid,
)
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
            object.__setattr__(
                self,
                name,
                MappingProxyType(
                    {
                        recipe_id: MappingProxyType(dict(answers))
                        for recipe_id, answers in values.items()
                    }
                ),
            )

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
            "project_id": self.project_id,
            "migration_run_id": self.migration_run_id,
            "plan_content_hash": self.plan_content_hash,
            "revision": self.revision,
            "parameters": self.by_recipe,
            "controls": self.controls_by_recipe,
            "updated_by": {
                "issuer": self.updated_by.issuer,
                "subject_id": self.updated_by.subject_id,
                "display_name": self.updated_by.display_name,
            },
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ProductionRunValues:
        if payload.get("contract_version") != 1:
            raise MigrationConflictError(
                "Saved Production values have an unsupported version"
            )
        actor = _object_mapping(payload.get("updated_by"), "updated_by")
        return cls(
            production_run_binding_id=_required_string(
                payload.get("production_run_binding_id"),
                "production_run_binding_id",
            ),
            project_id=_required_string(payload.get("project_id"), "project_id"),
            migration_run_id=_required_string(
                payload.get("migration_run_id"),
                "migration_run_id",
            ),
            plan_content_hash=_required_string(
                payload.get("plan_content_hash"),
                "plan_content_hash",
            ),
            revision=_integer(payload.get("revision"), "revision"),
            parameters=_parameters(payload.get("parameters")),
            controls=_controls(payload.get("controls")),
            updated_by=ActorIdentity(
                issuer=_required_string(actor.get("issuer"), "updated_by.issuer"),
                subject_id=_required_string(
                    actor.get("subject_id"),
                    "updated_by.subject_id",
                ),
                display_name=_required_string(
                    actor.get("display_name"),
                    "updated_by.display_name",
                ),
            ),
            updated_at=datetime.fromisoformat(
                _required_string(payload.get("updated_at"), "updated_at")
            ),
        )


def _object_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise TypeError(f"Saved Production {field_name} must be an object")
    return value


def _required_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"Saved Production {field_name} must be text")
    return value


def _integer(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Saved Production {field_name} must be an integer")
    return value


def _parameters(value: object) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for recipe_id, answers in _object_mapping(value, "parameters").items():
        result[recipe_id] = dict(_object_mapping(answers, "parameter answers"))
    return result


def _controls(value: object) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for recipe_id, answers in _object_mapping(value, "controls").items():
        answer_mapping = _object_mapping(answers, "control answers")
        if not all(isinstance(answer, str) for answer in answer_mapping.values()):
            raise TypeError("Saved Production control answers must be text")
        result[recipe_id] = {
            name: answer
            for name, answer in answer_mapping.items()
            if isinstance(answer, str)
        }
    return result
