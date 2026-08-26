"""Encode and restore restart-safe run-planning operation payloads."""

from __future__ import annotations

from collections.abc import Mapping

from ...migration_run_planning import (
    MigrationRunReferenceBundle,
    MigrationRunTargetSchema,
    OdooModelRequirement,
    PlannedRecipeApplication,
    ReferenceRequirement,
)


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
            "workspace": self._repository.foundation._workspace_dict(
                value.workspace
            ),
        }

    def stored_plan(self, detail: Mapping[str, object]):
        """Restore the exact run and application payload reserved for retry."""

        applications = tuple(
            PlannedRecipeApplication(
                application=self._repository._application_from_dict(
                    dict(item["application"])
                ),
                workspace=self._repository.foundation._workspace_from_dict(
                    dict(item["workspace"])
                ),
                dataset_ids=tuple(str(value) for value in item["dataset_ids"]),
                requirements=tuple(
                    OdooModelRequirement(
                        model=str(value["model"]),
                        fields=tuple(str(field) for field in value["fields"]),
                    )
                    for value in item["requirements"]
                ),
                reference_requirements=tuple(
                    ReferenceRequirement(
                        name=str(value["name"]),
                        content_hash=str(value["content_hash"]),
                    )
                    for value in item.get("reference_requirements", ())
                ),
                issues=tuple(
                    self._repository._issue_from_dict(value)
                    for value in item["issues"]
                ),
            )
            for item in detail["applications"]
        )
        return (
            self._repository.foundation._run_from_dict(dict(detail["run"])),
            self._repository._target_from_dict(dict(detail["target_binding"])),
            self._repository._plan_from_dict(dict(detail["requirement_plan"])),
            applications,
            MigrationRunTargetSchema.from_json(str(detail["target_schema_json"])),
            (
                MigrationRunReferenceBundle.from_dict(
                    dict(detail["reference_bundle"])
                )
                if detail.get("reference_bundle") is not None
                else None
            ),
        )
