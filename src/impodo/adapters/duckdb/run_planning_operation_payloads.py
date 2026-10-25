"""Encode and restore restart-safe run-planning operation payloads."""

from __future__ import annotations

from collections.abc import Mapping

from impodo.domain.run.contracts import (
    MigrationRunReferenceBundle,
    MigrationRunTargetSchema,
    OdooModelRequirement,
    PlannedRecipeApplication,
    ReferenceRequirement,
)


def _stored_mapping(value: object, label: str) -> dict[str, object]:
    """Validate one persisted JSON object before decoding its fields."""

    if not isinstance(value, Mapping):
        raise TypeError(f"Stored {label} must be an object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError(f"Stored {label} keys must be text")
        result[key] = item
    return result


def _stored_sequence(value: object, label: str) -> tuple[object, ...]:
    """Validate one persisted JSON array before decoding its entries."""

    if not isinstance(value, (list, tuple)):
        raise TypeError(f"Stored {label} must be a sequence")
    return tuple(value)


def _stored_strings(value: object, label: str) -> tuple[str, ...]:
    """Validate an exact persisted string array without coercing corruption."""

    items = _stored_sequence(value, label)
    if not all(isinstance(item, str) for item in items):
        raise ValueError(f"Stored {label} entries must be text")
    return tuple(item for item in items if isinstance(item, str))


class RunPlanningOperationPayloads:
    """Own the durable operation-intent representation of a planned run."""

    def __init__(self, repository) -> None:
        self._repository = repository

    def planned_dict(self, value: PlannedRecipeApplication) -> dict[str, object]:
        """Encode one planned application without changing its persisted shape."""

        return {
            "application": self._repository._application_dict(value.application),
            "dataset_ids": list(value.dataset_ids),
            "issues": [item.to_dict() for item in value.issues],
            "requirements": [item.to_dict() for item in value.requirements],
            "reference_requirements": [
                item.to_dict() for item in value.reference_requirements
            ],
            "workspace": self._repository.foundation._workspace_dict(value.workspace),
        }

    def stored_plan(self, detail: Mapping[str, object]):
        """Restore the exact run and application payload reserved for retry."""

        application_payloads = _stored_sequence(
            detail["applications"],
            "planned applications",
        )
        applications = tuple(
            PlannedRecipeApplication(
                application=self._repository._application_from_dict(
                    _stored_mapping(item["application"], "planned application")
                ),
                workspace=self._repository.foundation._workspace_from_dict(
                    _stored_mapping(item["workspace"], "planned workspace")
                ),
                dataset_ids=_stored_strings(
                    item["dataset_ids"],
                    "planned dataset IDs",
                ),
                requirements=tuple(
                    OdooModelRequirement(
                        model=str(requirement["model"]),
                        fields=_stored_strings(
                            requirement["fields"],
                            "model requirement fields",
                        ),
                    )
                    for requirement in (
                        _stored_mapping(value, "model requirement")
                        for value in _stored_sequence(
                            item["requirements"],
                            "model requirements",
                        )
                    )
                ),
                reference_requirements=tuple(
                    ReferenceRequirement(
                        name=str(requirement["name"]),
                        content_hash=str(requirement["content_hash"]),
                    )
                    for requirement in (
                        _stored_mapping(value, "reference requirement")
                        for value in _stored_sequence(
                            item.get("reference_requirements", ()),
                            "reference requirements",
                        )
                    )
                ),
                issues=tuple(
                    self._repository._issue_from_dict(
                        _stored_mapping(value, "planning issue")
                    )
                    for value in _stored_sequence(
                        item["issues"],
                        "planning issues",
                    )
                ),
            )
            for item in (
                _stored_mapping(value, "planned application payload")
                for value in application_payloads
            )
        )
        return (
            self._repository.foundation._run_from_dict(
                _stored_mapping(detail["run"], "migration run")
            ),
            self._repository._target_from_dict(
                _stored_mapping(detail["target_binding"], "target binding")
            ),
            self._repository._plan_from_dict(
                _stored_mapping(detail["requirement_plan"], "requirement plan")
            ),
            applications,
            MigrationRunTargetSchema.from_json(str(detail["target_schema_json"])),
            (
                MigrationRunReferenceBundle.from_dict(
                    _stored_mapping(
                        detail["reference_bundle"],
                        "reference bundle",
                    )
                )
                if detail.get("reference_bundle") is not None
                else None
            ),
        )
