"""Serialize registry roots, events, operation intents, and timestamps."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Mapping
from uuid import uuid4

import duckdb

from ...access import Actor, ActorIdentity
from ...domain.data_version.models import (
    DataVersion,
    DataVersionPurpose,
    DataVersionState,
)
from ...domain.project.models import (
    MigrationDataClassification,
    MigrationProject,
    MigrationProjectStatus,
)
from ...domain.run.models import (
    MigrationRun,
    MigrationRunPurpose,
    MigrationRunState,
)
from ...domain.serialization import canonical_json
from ...domain.workspace.models import (
    MigrationWorkspace,
    MigrationWorkspaceSetupState,
    MigrationWorkspaceState,
)
from ...migration_foundation import (
    FaultInjector,
    MigrationOperationIntent,
    MigrationOperationKind,
    MigrationOperationState,
)


class FoundationRecordCodecs:
    @staticmethod
    def _insert_event(
        connection: duckdb.DuckDBPyConnection,
        *,
        project_id: str,
        aggregate_kind: str,
        aggregate_id: str,
        aggregate_revision: int,
        event_type: str,
        detail: Mapping[str, object],
        actor: Actor,
        occurred_at: datetime,
    ) -> None:
        connection.execute(
            "INSERT INTO migration_event VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(uuid4()),
                project_id,
                aggregate_kind,
                aggregate_id,
                aggregate_revision,
                event_type,
                canonical_json(detail),
                actor.identity.issuer,
                actor.identity.subject_id,
                actor.identity.display_name,
                occurred_at.isoformat(),
            ],
        )

    @staticmethod
    def _fault(fault: FaultInjector | None, stage: str) -> None:
        if fault is not None:
            fault(stage)

    @staticmethod
    def _time(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    @staticmethod
    def _project_values(project: MigrationProject) -> list[object]:
        return [
            project.project_id,
            project.display_name,
            project.migration_purpose,
            project.source_system_identity,
            project.data_classification.value,
            project.retention_days,
            project.status.value,
            project.optimistic_revision,
            project.created_at.isoformat(),
            project.updated_at.isoformat(),
            FoundationRecordCodecs._time(project.closed_at),
            FoundationRecordCodecs._time(project.archived_at),
        ]

    @staticmethod
    def _project_dict(project: MigrationProject) -> dict[str, object]:
        return {
            "project_id": project.project_id,
            "display_name": project.display_name,
            "migration_purpose": project.migration_purpose,
            "source_system_identity": project.source_system_identity,
            "data_classification": project.data_classification.value,
            "retention_days": project.retention_days,
            "status": project.status.value,
            "optimistic_revision": project.optimistic_revision,
            "created_at": project.created_at.isoformat(),
            "updated_at": project.updated_at.isoformat(),
            "closed_at": FoundationRecordCodecs._time(project.closed_at),
            "archived_at": FoundationRecordCodecs._time(project.archived_at),
        }

    @staticmethod
    def _project_from_dict(value: Mapping[str, object]) -> MigrationProject:
        return MigrationProject(
            project_id=str(value["project_id"]),
            display_name=str(value["display_name"]),
            migration_purpose=str(value["migration_purpose"]),
            source_system_identity=str(value["source_system_identity"]),
            data_classification=MigrationDataClassification(
                str(value["data_classification"])
            ),
            retention_days=int(value["retention_days"]),
            status=MigrationProjectStatus(str(value["status"])),
            optimistic_revision=int(value["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
            closed_at=FoundationRecordCodecs._optional_time(value.get("closed_at")),
            archived_at=FoundationRecordCodecs._optional_time(value.get("archived_at")),
        )

    @classmethod
    def _project_from_row(cls, value: Mapping[str, object]) -> MigrationProject:
        return cls._project_from_dict(value)

    @staticmethod
    def _data_version_values(value: DataVersion) -> list[object]:
        return [
            value.data_version_id,
            value.project_id,
            value.version_number,
            value.parent_data_version_id,
            value.purpose.value,
            value.state.value,
            value.label,
            value.export_as_of,
            value.source_package_hash,
            value.optimistic_revision,
            value.created_at.isoformat(),
            value.updated_at.isoformat(),
            FoundationRecordCodecs._time(value.frozen_at),
        ]

    @staticmethod
    def _data_version_dict(value: DataVersion) -> dict[str, object]:
        return {
            "data_version_id": value.data_version_id,
            "project_id": value.project_id,
            "version_number": value.version_number,
            "parent_data_version_id": value.parent_data_version_id,
            "purpose": value.purpose.value,
            "state": value.state.value,
            "label": value.label,
            "export_as_of": value.export_as_of,
            "source_package_hash": value.source_package_hash,
            "optimistic_revision": value.optimistic_revision,
            "created_at": value.created_at.isoformat(),
            "updated_at": value.updated_at.isoformat(),
            "frozen_at": FoundationRecordCodecs._time(value.frozen_at),
        }

    @staticmethod
    def _data_version_from_dict(value: Mapping[str, object]) -> DataVersion:
        return DataVersion(
            data_version_id=str(value["data_version_id"]),
            project_id=str(value["project_id"]),
            version_number=int(value["version_number"]),
            parent_data_version_id=(
                str(value["parent_data_version_id"])
                if value.get("parent_data_version_id")
                else None
            ),
            purpose=DataVersionPurpose(str(value["purpose"])),
            state=DataVersionState(str(value["state"])),
            label=str(value["label"]),
            export_as_of=str(value["export_as_of"]),
            source_package_hash=(
                str(value["source_package_hash"])
                if value.get("source_package_hash")
                else None
            ),
            optimistic_revision=int(value["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
            frozen_at=FoundationRecordCodecs._optional_time(value.get("frozen_at")),
        )

    @classmethod
    def _data_version_from_row(cls, value: Mapping[str, object]) -> DataVersion:
        return cls._data_version_from_dict(value)

    @staticmethod
    def _run_values(value: MigrationRun) -> list[object]:
        return [
            value.migration_run_id,
            value.project_id,
            value.data_version_id,
            value.run_number,
            value.purpose.value,
            value.label,
            value.state.value,
            value.target_binding_id,
            value.cutover_selection_id,
            value.optimistic_revision,
            value.created_at.isoformat(),
            value.updated_at.isoformat(),
            FoundationRecordCodecs._time(value.closed_at),
        ]

    @staticmethod
    def _run_dict(value: MigrationRun) -> dict[str, object]:
        return {
            "migration_run_id": value.migration_run_id,
            "project_id": value.project_id,
            "data_version_id": value.data_version_id,
            "run_number": value.run_number,
            "purpose": value.purpose.value,
            "label": value.label,
            "state": value.state.value,
            "target_binding_id": value.target_binding_id,
            "cutover_selection_id": value.cutover_selection_id,
            "optimistic_revision": value.optimistic_revision,
            "created_at": value.created_at.isoformat(),
            "updated_at": value.updated_at.isoformat(),
            "closed_at": FoundationRecordCodecs._time(value.closed_at),
        }

    @staticmethod
    def _run_from_dict(value: Mapping[str, object]) -> MigrationRun:
        return MigrationRun(
            migration_run_id=str(value["migration_run_id"]),
            project_id=str(value["project_id"]),
            data_version_id=str(value["data_version_id"]),
            run_number=int(value["run_number"]),
            purpose=MigrationRunPurpose(str(value["purpose"])),
            label=str(value["label"]),
            state=MigrationRunState(str(value["state"])),
            target_binding_id=(
                str(value["target_binding_id"])
                if value.get("target_binding_id")
                else None
            ),
            cutover_selection_id=(
                str(value["cutover_selection_id"])
                if value.get("cutover_selection_id")
                else None
            ),
            optimistic_revision=int(value["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
            closed_at=FoundationRecordCodecs._optional_time(value.get("closed_at")),
        )

    @classmethod
    def _run_from_row(cls, value: Mapping[str, object]) -> MigrationRun:
        return cls._run_from_dict(value)

    @staticmethod
    def _workspace_values(value: MigrationWorkspace) -> list[object]:
        return [
            value.workspace_id,
            value.project_id,
            value.data_version_id,
            value.migration_run_id,
            value.recipe_application_id,
            value.display_name,
            value.state.value,
            value.setup_state.value,
            value.optimistic_revision,
            value.created_at.isoformat(),
            value.updated_at.isoformat(),
            FoundationRecordCodecs._time(value.setup_completed_at),
            FoundationRecordCodecs._time(value.closed_at),
        ]

    @staticmethod
    def _workspace_dict(value: MigrationWorkspace) -> dict[str, object]:
        return {
            "workspace_id": value.workspace_id,
            "project_id": value.project_id,
            "data_version_id": value.data_version_id,
            "migration_run_id": value.migration_run_id,
            "recipe_application_id": value.recipe_application_id,
            "display_name": value.display_name,
            "state": value.state.value,
            "setup_state": value.setup_state.value,
            "optimistic_revision": value.optimistic_revision,
            "created_at": value.created_at.isoformat(),
            "updated_at": value.updated_at.isoformat(),
            "setup_completed_at": FoundationRecordCodecs._time(
                value.setup_completed_at
            ),
            "closed_at": FoundationRecordCodecs._time(value.closed_at),
        }

    @staticmethod
    def _workspace_from_dict(value: Mapping[str, object]) -> MigrationWorkspace:
        return MigrationWorkspace(
            workspace_id=str(value["workspace_id"]),
            project_id=str(value["project_id"]),
            data_version_id=str(value["data_version_id"]),
            migration_run_id=str(value["migration_run_id"]),
            recipe_application_id=(
                str(value["recipe_application_id"])
                if value.get("recipe_application_id")
                else None
            ),
            display_name=str(value["display_name"]),
            state=MigrationWorkspaceState(str(value["state"])),
            setup_state=MigrationWorkspaceSetupState(str(value["setup_state"])),
            optimistic_revision=int(value["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
            setup_completed_at=FoundationRecordCodecs._optional_time(
                value.get("setup_completed_at")
            ),
            closed_at=FoundationRecordCodecs._optional_time(value.get("closed_at")),
        )

    @classmethod
    def _workspace_from_row(
        cls,
        value: Mapping[str, object],
    ) -> MigrationWorkspace:
        return cls._workspace_from_dict(value)

    @staticmethod
    def _intent_from_row(value: Mapping[str, object]) -> MigrationOperationIntent:
        return MigrationOperationIntent(
            operation_id=str(value["operation_id"]),
            project_id=str(value["project_id"]),
            owner_kind=str(value["owner_kind"]),
            owner_id=str(value["owner_id"]),
            kind=MigrationOperationKind(str(value["kind"])),
            request_hash=str(value["request_hash"]),
            expected_revision=(
                int(value["expected_revision"])
                if value.get("expected_revision") is not None
                else None
            ),
            state=MigrationOperationState(str(value["state"])),
            stage=str(value["stage"]),
            detail=json.loads(str(value["detail_json"])),
            last_error=str(value["last_error"]),
            actor=ActorIdentity(
                issuer=str(value["actor_issuer"]),
                subject_id=str(value["actor_subject"]),
                display_name=str(value["actor_display_name"]),
            ),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
        )

    @staticmethod
    def _optional_time(value: object) -> datetime | None:
        return datetime.fromisoformat(str(value)) if value else None
