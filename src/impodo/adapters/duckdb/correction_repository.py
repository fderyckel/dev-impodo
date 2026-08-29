"""Persist one run-owned correction binding and its current evidence pointers."""

from __future__ import annotations

from datetime import datetime, timezone
from impodo.application.correction_orchestration import (
    CorrectionBinding,
)
from impodo.domain.correction_origin import (
    CorrectionOriginError,
    ProtectedCorrectionArtifactReference,
)
from impodo.domain.project.foundation import (
    FaultInjector,
    MigrationConflictError,
    require_hash,
    require_revision,
    require_uuid,
)
from impodo.domain.shared.access import Actor
from .migration_foundation_repository import MigrationFoundationRepository


class CorrectionRepository:
    """Use the shared registry transaction as the only correction pointer owner."""

    def __init__(self, foundation: MigrationFoundationRepository) -> None:
        self.foundation = foundation

    def get_for_completed_workspace(
        self,
        completed_workspace_id: str,
    ) -> CorrectionBinding | None:
        completed_workspace_id = require_uuid(
            completed_workspace_id,
            "completed_workspace_id",
        )
        with self.foundation.database.connect(
            self.foundation.registry_path
        ) as connection:
            row = connection.execute(
                "SELECT * FROM correction_run_binding "
                "WHERE completed_workspace_id = ?",
                [completed_workspace_id],
            ).fetchone()
            columns = (
                tuple(item[0] for item in connection.description)
                if row is not None
                else ()
            )
        return self._binding(dict(zip(columns, row, strict=True))) if row else None

    def get_for_successor_workspace(
        self,
        successor_workspace_id: str,
    ) -> CorrectionBinding | None:
        """Return the correction binding that owns one successor workspace."""

        successor_workspace_id = require_uuid(
            successor_workspace_id,
            "successor_workspace_id",
        )
        with self.foundation.database.connect(
            self.foundation.registry_path
        ) as connection:
            row = connection.execute(
                "SELECT * FROM correction_run_binding "
                "WHERE successor_workspace_id = ?",
                [successor_workspace_id],
            ).fetchone()
            columns = (
                tuple(item[0] for item in connection.description)
                if row is not None
                else ()
            )
        return self._binding(dict(zip(columns, row, strict=True))) if row else None

    def list_for_project(self, project_id: str) -> tuple[CorrectionBinding, ...]:
        """Return bounded correction pointers for one Project overview."""

        project_id = require_uuid(project_id, "project_id")
        with self.foundation.database.connect(
            self.foundation.registry_path
        ) as connection:
            rows = connection.execute(
                "SELECT * FROM correction_run_binding "
                "WHERE project_id = ? ORDER BY created_at DESC",
                [project_id],
            ).fetchall()
            columns = tuple(item[0] for item in connection.description)
        return tuple(
            self._binding(dict(zip(columns, row, strict=True))) for row in rows
        )

    def invalidate_successor_mapping(
        self,
        workspace_id: str,
        *,
        mapping_hash: str,
        actor: Actor,
    ) -> None:
        """Clear stale prepared/plan pointers before a mapping draft changes."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        mapping_hash = require_hash(mapping_hash, "mapping_hash")
        with self.foundation._registry_transactions.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM correction_run_binding "
                "WHERE successor_workspace_id = ?",
                [workspace_id],
            ).fetchone()
            if row is None:
                return
            columns = tuple(item[0] for item in connection.description)
            current = self._binding(dict(zip(columns, row, strict=True)))
            if current.current_mapping_hash == mapping_hash:
                return
            now = datetime.now(timezone.utc)
            revision = current.optimistic_revision
            connection.execute(
                """
                UPDATE correction_run_binding
                   SET current_mapping_hash = ?, current_prepared_hash = NULL,
                       current_plan_id = NULL, current_plan_hash = NULL,
                       current_plan_storage_key = NULL,
                       current_plan_artifact_hash = NULL,
                       current_confirmation_id = NULL,
                       current_confirmation_hash = NULL,
                       current_confirmation_storage_key = NULL,
                       current_confirmation_artifact_hash = NULL,
                       optimistic_revision = ?, updated_at = ?
                 WHERE successor_workspace_id = ? AND optimistic_revision = ?
                """,
                [
                    mapping_hash,
                    revision + 1,
                    now.isoformat(),
                    workspace_id,
                    revision,
                ],
            )
            self._event(
                connection,
                current,
                aggregate_kind="CORRECTION_BINDING",
                aggregate_id=current.correction_binding_id,
                revision=revision + 1,
                event_type="CORRECTION_MAPPING_CHANGED",
                actor=actor,
                occurred_at=now,
            )

    def seal_completed_origin(
        self,
        binding: CorrectionBinding,
        *,
        expected_run_revision: int,
        expected_workspace_revision: int,
        actor: Actor,
        fault: FaultInjector | None = None,
    ) -> CorrectionBinding:
        """Publish origin visibility and close the completed owners atomically."""

        expected_run_revision = require_revision(expected_run_revision)
        expected_workspace_revision = require_revision(expected_workspace_revision)
        with self.foundation._registry_transactions.transaction() as connection:
            existing = self._read_binding(
                connection,
                binding.completed_workspace_id,
            )
            if existing is not None:
                if not self._same_origin(existing, binding):
                    raise MigrationConflictError(
                        "Completed load already has another correction origin"
                    )
                return existing
            run = connection.execute(
                """
                SELECT project_id, data_version_id, purpose, state,
                       optimistic_revision
                  FROM migration_run
                 WHERE migration_run_id = ?
                """,
                [binding.completed_migration_run_id],
            ).fetchone()
            workspace = connection.execute(
                """
                SELECT project_id, data_version_id, migration_run_id, state,
                       optimistic_revision
                  FROM migration_workspace
                 WHERE workspace_id = ?
                """,
                [binding.completed_workspace_id],
            ).fetchone()
            if (
                run is None
                or workspace is None
                or tuple(str(item) for item in run[:2])
                != (binding.project_id, binding.data_version_id)
                or str(run[2]) != "AUTHORING"
                or str(run[3]) == "CLOSED"
                or int(run[4]) != expected_run_revision
                or tuple(str(item) for item in workspace[:3])
                != (
                    binding.project_id,
                    binding.data_version_id,
                    binding.completed_migration_run_id,
                )
                or str(workspace[3]) != "OPEN"
                or int(workspace[4]) != expected_workspace_revision
            ):
                raise MigrationConflictError(
                    "Completed load changed before correction origin publication"
                )
            now = binding.created_at.astimezone(timezone.utc)
            connection.execute(
                """
                INSERT INTO correction_run_binding VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                    NULL, NULL, NULL, NULL, ?, ?, ?, 1
                )
                """,
                [
                    binding.correction_binding_id,
                    binding.project_id,
                    binding.data_version_id,
                    binding.completed_migration_run_id,
                    binding.completed_workspace_id,
                    binding.origin.artifact_id,
                    binding.origin.logical_hash,
                    binding.origin.storage_key,
                    binding.origin.artifact_hash,
                    binding.target_index.artifact_id,
                    binding.target_index.logical_hash,
                    binding.target_index.storage_key,
                    binding.target_index.artifact_hash,
                    binding.optimistic_revision,
                    now.isoformat(),
                    now.isoformat(),
                ],
            )
            connection.execute(
                """
                UPDATE migration_run
                   SET state = 'COMPLETED', optimistic_revision = ?, updated_at = ?
                 WHERE migration_run_id = ? AND optimistic_revision = ?
                """,
                [
                    expected_run_revision + 1,
                    now.isoformat(),
                    binding.completed_migration_run_id,
                    expected_run_revision,
                ],
            )
            connection.execute(
                """
                UPDATE migration_workspace
                   SET state = 'CLOSED', optimistic_revision = ?, updated_at = ?,
                       closed_at = ?
                 WHERE workspace_id = ? AND optimistic_revision = ?
                """,
                [
                    expected_workspace_revision + 1,
                    now.isoformat(),
                    now.isoformat(),
                    binding.completed_workspace_id,
                    expected_workspace_revision,
                ],
            )
            self._event(
                connection,
                binding,
                aggregate_kind="MIGRATION_RUN",
                aggregate_id=binding.completed_migration_run_id,
                revision=expected_run_revision + 1,
                event_type="MIGRATION_RUN_COMPLETED_FOR_CORRECTION",
                actor=actor,
                occurred_at=now,
            )
            self._event(
                connection,
                binding,
                aggregate_kind="MIGRATION_WORKSPACE",
                aggregate_id=binding.completed_workspace_id,
                revision=expected_workspace_revision + 1,
                event_type="CORRECTION_ORIGIN_PUBLISHED",
                actor=actor,
                occurred_at=now,
            )
            if fault is not None:
                fault("BEFORE_REGISTRY_COMMIT")
        if fault is not None:
            fault("REGISTRY_COMMITTED")
        result = self.get_for_completed_workspace(binding.completed_workspace_id)
        if result is None:
            raise CorrectionOriginError("Published correction origin is missing")
        return result

    def attach_successor(
        self,
        completed_workspace_id: str,
        *,
        successor_migration_run_id: str,
        successor_workspace_id: str,
        expected_revision: int,
        actor: Actor,
    ) -> CorrectionBinding:
        completed_workspace_id = require_uuid(
            completed_workspace_id,
            "completed_workspace_id",
        )
        successor_migration_run_id = require_uuid(
            successor_migration_run_id,
            "successor_migration_run_id",
        )
        successor_workspace_id = require_uuid(
            successor_workspace_id,
            "successor_workspace_id",
        )
        with self.foundation._registry_transactions.transaction() as connection:
            current = self._require_current(
                connection,
                completed_workspace_id,
            )
            if current.successor_migration_run_id is not None:
                if (
                    current.successor_migration_run_id
                    != successor_migration_run_id
                    or current.successor_workspace_id != successor_workspace_id
                ):
                    raise MigrationConflictError(
                        "Correction origin already has another successor"
                    )
                return current
            if current.optimistic_revision != require_revision(expected_revision):
                raise MigrationConflictError("Correction binding revision is stale")
            lineage = connection.execute(
                """
                SELECT r.project_id, r.data_version_id, r.purpose, r.state,
                       w.project_id, w.data_version_id, w.migration_run_id, w.state
                  FROM migration_run r
                  JOIN migration_workspace w ON w.workspace_id = ?
                 WHERE r.migration_run_id = ?
                """,
                [successor_workspace_id, successor_migration_run_id],
            ).fetchone()
            if (
                lineage is None
                or tuple(str(item) for item in lineage[:2])
                != (current.project_id, current.data_version_id)
                or str(lineage[2]) != "AUTHORING"
                or str(lineage[3]) in {"COMPLETED", "CLOSED"}
                or tuple(str(item) for item in lineage[4:7])
                != (
                    current.project_id,
                    current.data_version_id,
                    successor_migration_run_id,
                )
                or str(lineage[7]) != "OPEN"
            ):
                raise MigrationConflictError(
                    "Correction successor does not preserve origin lineage"
                )
            now = datetime.now(timezone.utc)
            connection.execute(
                """
                UPDATE correction_run_binding
                   SET successor_migration_run_id = ?, successor_workspace_id = ?,
                       optimistic_revision = ?, updated_at = ?
                 WHERE completed_workspace_id = ? AND optimistic_revision = ?
                """,
                [
                    successor_migration_run_id,
                    successor_workspace_id,
                    expected_revision + 1,
                    now.isoformat(),
                    completed_workspace_id,
                    expected_revision,
                ],
            )
            self._event(
                connection,
                current,
                aggregate_kind="CORRECTION_BINDING",
                aggregate_id=current.correction_binding_id,
                revision=expected_revision + 1,
                event_type="CORRECTION_SUCCESSOR_ATTACHED",
                actor=actor,
                occurred_at=now,
            )
        return self._require_result(completed_workspace_id)

    def publish_plan(
        self,
        completed_workspace_id: str,
        *,
        successor_workspace_id: str,
        mapping_hash: str,
        prepared_hash: str,
        plan: ProtectedCorrectionArtifactReference,
        expected_revision: int,
        actor: Actor,
    ) -> CorrectionBinding:
        completed_workspace_id = require_uuid(
            completed_workspace_id,
            "completed_workspace_id",
        )
        require_uuid(successor_workspace_id, "successor_workspace_id")
        require_hash(mapping_hash, "mapping_hash")
        require_hash(prepared_hash, "prepared_hash")
        with self.foundation._registry_transactions.transaction() as connection:
            current = self._require_current(connection, completed_workspace_id)
            if (
                current.current_plan == plan
                and current.current_mapping_hash == mapping_hash
                and current.current_prepared_hash == prepared_hash
            ):
                return current
            if (
                current.optimistic_revision != require_revision(expected_revision)
                or current.successor_workspace_id != successor_workspace_id
            ):
                raise MigrationConflictError("Correction review is stale")
            now = datetime.now(timezone.utc)
            connection.execute(
                """
                UPDATE correction_run_binding
                   SET current_mapping_hash = ?, current_prepared_hash = ?,
                       current_plan_id = ?, current_plan_hash = ?,
                       current_plan_storage_key = ?,
                       current_plan_artifact_hash = ?,
                       current_confirmation_id = NULL,
                       current_confirmation_hash = NULL,
                       current_confirmation_storage_key = NULL,
                       current_confirmation_artifact_hash = NULL,
                       optimistic_revision = ?, updated_at = ?
                 WHERE completed_workspace_id = ? AND optimistic_revision = ?
                """,
                [
                    mapping_hash,
                    prepared_hash,
                    plan.artifact_id,
                    plan.logical_hash,
                    plan.storage_key,
                    plan.artifact_hash,
                    expected_revision + 1,
                    now.isoformat(),
                    completed_workspace_id,
                    expected_revision,
                ],
            )
            self._event(
                connection,
                current,
                aggregate_kind="CORRECTION_BINDING",
                aggregate_id=current.correction_binding_id,
                revision=expected_revision + 1,
                event_type="CORRECTION_PLAN_PUBLISHED",
                actor=actor,
                occurred_at=now,
            )
        return self._require_result(completed_workspace_id)

    def invalidate_plan(
        self,
        completed_workspace_id: str,
        *,
        current_mapping_hash: str | None,
        current_prepared_hash: str | None,
        expected_revision: int,
        actor: Actor,
    ) -> CorrectionBinding:
        completed_workspace_id = require_uuid(
            completed_workspace_id,
            "completed_workspace_id",
        )
        if current_mapping_hash is not None:
            require_hash(current_mapping_hash, "current_mapping_hash")
        if current_prepared_hash is not None:
            require_hash(current_prepared_hash, "current_prepared_hash")
        with self.foundation._registry_transactions.transaction() as connection:
            current = self._require_current(connection, completed_workspace_id)
            if (
                current.current_plan is None
                and current.current_confirmation is None
                and current.current_mapping_hash == current_mapping_hash
                and current.current_prepared_hash == current_prepared_hash
            ):
                return current
            if current.optimistic_revision != require_revision(expected_revision):
                raise MigrationConflictError("Correction invalidation is stale")
            now = datetime.now(timezone.utc)
            connection.execute(
                """
                UPDATE correction_run_binding
                   SET current_mapping_hash = ?, current_prepared_hash = ?,
                       current_plan_id = NULL, current_plan_hash = NULL,
                       current_plan_storage_key = NULL,
                       current_plan_artifact_hash = NULL,
                       current_confirmation_id = NULL,
                       current_confirmation_hash = NULL,
                       current_confirmation_storage_key = NULL,
                       current_confirmation_artifact_hash = NULL,
                       optimistic_revision = ?, updated_at = ?
                 WHERE completed_workspace_id = ? AND optimistic_revision = ?
                """,
                [
                    current_mapping_hash,
                    current_prepared_hash,
                    expected_revision + 1,
                    now.isoformat(),
                    completed_workspace_id,
                    expected_revision,
                ],
            )
            self._event(
                connection,
                current,
                aggregate_kind="CORRECTION_BINDING",
                aggregate_id=current.correction_binding_id,
                revision=expected_revision + 1,
                event_type="CORRECTION_PLAN_INVALIDATED",
                actor=actor,
                occurred_at=now,
            )
        return self._require_result(completed_workspace_id)

    def publish_confirmation(
        self,
        completed_workspace_id: str,
        *,
        successor_workspace_id: str,
        plan_id: str,
        plan_hash: str,
        confirmation: ProtectedCorrectionArtifactReference,
        expected_revision: int,
        actor: Actor,
    ) -> CorrectionBinding:
        """Publish explicit confirmation only for the current protected plan."""

        completed_workspace_id = require_uuid(
            completed_workspace_id, "completed_workspace_id"
        )
        require_uuid(successor_workspace_id, "successor_workspace_id")
        require_uuid(plan_id, "plan_id")
        require_hash(plan_hash, "plan_hash")
        with self.foundation._registry_transactions.transaction() as connection:
            current = self._require_current(connection, completed_workspace_id)
            if current.current_confirmation == confirmation:
                return current
            if (
                current.optimistic_revision != require_revision(expected_revision)
                or current.successor_workspace_id != successor_workspace_id
                or current.current_plan is None
                or current.current_plan.artifact_id != plan_id
                or current.current_plan.logical_hash != plan_hash
            ):
                raise MigrationConflictError("Correction confirmation is stale")
            now = datetime.now(timezone.utc)
            connection.execute(
                """
                UPDATE correction_run_binding
                   SET current_confirmation_id = ?,
                       current_confirmation_hash = ?,
                       current_confirmation_storage_key = ?,
                       current_confirmation_artifact_hash = ?,
                       optimistic_revision = ?, updated_at = ?
                 WHERE completed_workspace_id = ? AND optimistic_revision = ?
                """,
                [
                    confirmation.artifact_id,
                    confirmation.logical_hash,
                    confirmation.storage_key,
                    confirmation.artifact_hash,
                    expected_revision + 1,
                    now.isoformat(),
                    completed_workspace_id,
                    expected_revision,
                ],
            )
            self._event(
                connection,
                current,
                aggregate_kind="CORRECTION_BINDING",
                aggregate_id=current.correction_binding_id,
                revision=expected_revision + 1,
                event_type="CORRECTION_CONFIRMED",
                actor=actor,
                occurred_at=now,
            )
        return self._require_result(completed_workspace_id)

    def complete_verified_successor(
        self,
        completed_workspace_id: str,
        *,
        successor_migration_run_id: str,
        successor_workspace_id: str,
        execution_run_id: str,
        reconciliation_id: str,
        reconciliation_hash: str,
        expected_revision: int,
        actor: Actor,
    ) -> CorrectionBinding:
        """Close the successor owners after a current VERIFIED reconciliation."""

        completed_workspace_id = require_uuid(
            completed_workspace_id, "completed_workspace_id"
        )
        successor_migration_run_id = require_uuid(
            successor_migration_run_id, "successor_migration_run_id"
        )
        successor_workspace_id = require_uuid(
            successor_workspace_id, "successor_workspace_id"
        )
        require_uuid(execution_run_id, "execution_run_id")
        require_uuid(reconciliation_id, "reconciliation_id")
        require_hash(reconciliation_hash, "reconciliation_hash")
        with self.foundation._registry_transactions.transaction() as connection:
            current = self._require_current(connection, completed_workspace_id)
            lineage = connection.execute(
                """
                SELECT r.state, r.optimistic_revision,
                       w.state, w.optimistic_revision
                  FROM migration_run r
                  JOIN migration_workspace w ON w.workspace_id = ?
                 WHERE r.migration_run_id = ?
                   AND w.migration_run_id = r.migration_run_id
                """,
                [successor_workspace_id, successor_migration_run_id],
            ).fetchone()
            binding_matches = (
                current.successor_migration_run_id == successor_migration_run_id
                and current.successor_workspace_id == successor_workspace_id
                and current.current_plan is not None
                and current.current_confirmation is not None
            )
            if (
                binding_matches
                and lineage is not None
                and tuple(str(item) for item in lineage[::2])
                == ("COMPLETED", "CLOSED")
            ):
                return current
            if (
                current.optimistic_revision != require_revision(expected_revision)
                or not binding_matches
                or lineage is None
                or str(lineage[0]) in {"COMPLETED", "CLOSED"}
                or str(lineage[2]) != "OPEN"
            ):
                raise MigrationConflictError("Correction completion is stale")
            now = datetime.now(timezone.utc)
            run_revision = int(lineage[1])
            workspace_revision = int(lineage[3])
            connection.execute(
                """UPDATE migration_run
                      SET state = 'COMPLETED', optimistic_revision = ?, updated_at = ?
                    WHERE migration_run_id = ? AND optimistic_revision = ?""",
                [
                    run_revision + 1,
                    now.isoformat(),
                    successor_migration_run_id,
                    run_revision,
                ],
            )
            connection.execute(
                """UPDATE migration_workspace
                      SET state = 'CLOSED', optimistic_revision = ?,
                          updated_at = ?, closed_at = ?
                    WHERE workspace_id = ? AND optimistic_revision = ?""",
                [
                    workspace_revision + 1,
                    now.isoformat(),
                    now.isoformat(),
                    successor_workspace_id,
                    workspace_revision,
                ],
            )
            connection.execute(
                """UPDATE correction_run_binding
                      SET optimistic_revision = ?, updated_at = ?
                    WHERE completed_workspace_id = ? AND optimistic_revision = ?""",
                [
                    expected_revision + 1,
                    now.isoformat(),
                    completed_workspace_id,
                    expected_revision,
                ],
            )
            self._event(
                connection,
                current,
                aggregate_kind="CORRECTION_BINDING",
                aggregate_id=current.correction_binding_id,
                revision=expected_revision + 1,
                event_type="CORRECTION_VERIFIED",
                actor=actor,
                occurred_at=now,
            )
        return self._require_result(completed_workspace_id)

    def _require_result(self, completed_workspace_id: str) -> CorrectionBinding:
        result = self.get_for_completed_workspace(completed_workspace_id)
        if result is None:
            raise CorrectionOriginError("Correction binding is missing")
        return result

    def _require_current(self, connection, completed_workspace_id: str):
        current = self._read_binding(connection, completed_workspace_id)
        if current is None:
            raise CorrectionOriginError("Correction binding is missing")
        return current

    def _read_binding(self, connection, completed_workspace_id: str):
        row = connection.execute(
            "SELECT * FROM correction_run_binding "
            "WHERE completed_workspace_id = ?",
            [completed_workspace_id],
        ).fetchone()
        if row is None:
            return None
        columns = tuple(item[0] for item in connection.description)
        return self._binding(dict(zip(columns, row, strict=True)))

    @staticmethod
    def _binding(value: dict[str, object]) -> CorrectionBinding:
        def reference(prefix: str):
            artifact_id = value[f"{prefix}_id"]
            if artifact_id is None:
                return None
            return ProtectedCorrectionArtifactReference(
                artifact_id=str(artifact_id),
                logical_hash=str(value[f"{prefix}_hash"]),
                storage_key=str(value[f"{prefix}_storage_key"]),
                artifact_hash=str(value[f"{prefix}_artifact_hash"]),
            )

        return CorrectionBinding(
            correction_binding_id=str(value["correction_binding_id"]),
            project_id=str(value["project_id"]),
            data_version_id=str(value["data_version_id"]),
            completed_migration_run_id=str(value["completed_migration_run_id"]),
            completed_workspace_id=str(value["completed_workspace_id"]),
            origin=ProtectedCorrectionArtifactReference(
                artifact_id=str(value["origin_manifest_id"]),
                logical_hash=str(value["origin_manifest_hash"]),
                storage_key=str(value["origin_storage_key"]),
                artifact_hash=str(value["origin_artifact_hash"]),
            ),
            target_index=ProtectedCorrectionArtifactReference(
                artifact_id=str(value["target_index_id"]),
                logical_hash=str(value["target_index_hash"]),
                storage_key=str(value["target_index_storage_key"]),
                artifact_hash=str(value["target_index_artifact_hash"]),
            ),
            successor_migration_run_id=(
                str(value["successor_migration_run_id"])
                if value["successor_migration_run_id"] is not None
                else None
            ),
            successor_workspace_id=(
                str(value["successor_workspace_id"])
                if value["successor_workspace_id"] is not None
                else None
            ),
            current_mapping_hash=(
                str(value["current_mapping_hash"])
                if value["current_mapping_hash"] is not None
                else None
            ),
            current_prepared_hash=(
                str(value["current_prepared_hash"])
                if value["current_prepared_hash"] is not None
                else None
            ),
            current_plan=reference("current_plan"),
            current_confirmation=reference("current_confirmation"),
            optimistic_revision=int(value["optimistic_revision"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
            updated_at=datetime.fromisoformat(str(value["updated_at"])),
        )

    @staticmethod
    def _same_origin(left: CorrectionBinding, right: CorrectionBinding) -> bool:
        return (
            left.correction_binding_id == right.correction_binding_id
            and left.project_id == right.project_id
            and left.data_version_id == right.data_version_id
            and left.completed_migration_run_id
            == right.completed_migration_run_id
            and left.completed_workspace_id == right.completed_workspace_id
            and left.origin == right.origin
            and left.target_index == right.target_index
        )

    def _event(
        self,
        connection,
        binding: CorrectionBinding,
        *,
        aggregate_kind: str,
        aggregate_id: str | None,
        revision: int,
        event_type: str,
        actor: Actor,
        occurred_at: datetime,
    ) -> None:
        if aggregate_id is None:
            raise CorrectionOriginError("Correction event owner is missing")
        self.foundation._insert_event(
            connection,
            project_id=binding.project_id,
            aggregate_kind=aggregate_kind,
            aggregate_id=aggregate_id,
            aggregate_revision=revision,
            event_type=event_type,
            detail={
                "correction_binding_id": binding.correction_binding_id,
                "origin_manifest_hash": binding.origin.logical_hash,
            },
            actor=actor,
            occurred_at=occurred_at,
        )


__all__ = ["CorrectionRepository"]
