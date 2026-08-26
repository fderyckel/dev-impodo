"""Reserve, advance, and commit restart-safe registry operation intents."""

from __future__ import annotations

from typing import Mapping

import duckdb

from impodo.domain.shared.access import Actor
from ...domain.serialization import canonical_json
from impodo.domain.project.foundation import (
    MigrationConflictError,
    MigrationNotFoundError,
    MigrationOperationIntent,
    MigrationOperationKind,
    MigrationOperationReplayError,
    MigrationOperationState,
    require_hash,
    require_revision,
    require_uuid,
    utc_now,
)


class FoundationOperationIntents:
    def get_operation_intent(self, operation_id: str) -> MigrationOperationIntent:
        operation_id = require_uuid(operation_id, "operation_id")
        with self.database.connect(self.registry_path) as connection:
            rows = self._rows(
                connection,
                "SELECT * FROM project_operation_intent WHERE operation_id = ?",
                [operation_id],
            )
        if not rows:
            raise MigrationNotFoundError("Operation intent not found")
        return self._intent_from_row(rows[0])

    def _pending_create_intent(
        self,
        operation_id: str,
        expected_kind: MigrationOperationKind,
        expected_owner_kind: str,
    ) -> MigrationOperationIntent:
        intent = self.get_operation_intent(operation_id)
        if intent.kind is not expected_kind or intent.owner_kind != expected_owner_kind:
            raise MigrationOperationReplayError(
                "Operation identity does not belong to this creation command"
            )
        return intent

    def _reserve_intent(
        self,
        *,
        operation_id: str,
        project_id: str,
        owner_kind: str,
        owner_id: str,
        kind: MigrationOperationKind,
        request_hash: str,
        expected_revision: int | None,
        detail: Mapping[str, object],
        actor: Actor,
    ) -> MigrationOperationIntent:
        operation_id = require_uuid(operation_id, "operation_id")
        project_id = require_uuid(project_id, "project_id")
        owner_id = require_uuid(owner_id, "owner_id")
        request_hash = require_hash(request_hash, "request_hash")
        if expected_revision is not None:
            expected_revision = require_revision(
                expected_revision,
                "expected_revision",
            )
        with self.database.connect(self.registry_path) as connection:
            rows = self._rows(
                connection,
                "SELECT * FROM project_operation_intent WHERE operation_id = ?",
                [operation_id],
            )
            if rows:
                current = self._intent_from_row(rows[0])
                if (
                    (
                        current.project_id != project_id
                        and kind is not MigrationOperationKind.PROJECT_CREATE
                    )
                    or current.owner_kind != owner_kind
                    or current.kind is not kind
                    or current.request_hash != request_hash
                    or current.expected_revision != expected_revision
                    or current.actor.issuer != actor.identity.issuer
                    or current.actor.subject_id != actor.identity.subject_id
                ):
                    raise MigrationOperationReplayError(
                        "Operation identity was already used with different meaning"
                    )
                return current
            now = utc_now()
            connection.execute(
                """
                INSERT INTO project_operation_intent VALUES (
                    ?, ?, ?, ?, ?, ?, ?, 'PENDING', 'INTENT_RESERVED',
                    ?, '{}', '', ?, ?, ?, ?, ?
                )
                """,
                [
                    operation_id,
                    project_id,
                    owner_kind,
                    owner_id,
                    kind.value,
                    request_hash,
                    expected_revision,
                    canonical_json(detail),
                    actor.identity.issuer,
                    actor.identity.subject_id,
                    actor.identity.display_name,
                    now.isoformat(),
                    now.isoformat(),
                ],
            )
        return self.get_operation_intent(operation_id)

    def _finish_pending_intent(
        self,
        operation_id: str,
        *,
        stage: str,
        result: Mapping[str, object],
    ) -> None:
        with self.database.connect(self.registry_path) as connection:
            updated = connection.execute(
                """
                UPDATE project_operation_intent
                   SET state = 'COMMITTED', stage = ?, result_json = ?,
                       last_error = '', updated_at = ?
                 WHERE operation_id = ? AND state = 'PENDING'
                 RETURNING operation_id
                """,
                [stage, canonical_json(result), utc_now().isoformat(), operation_id],
            ).fetchone()
        if updated is None:
            current = self.get_operation_intent(operation_id)
            if current.state is not MigrationOperationState.COMMITTED:
                raise MigrationConflictError("Operation intent cannot commit")

    @staticmethod
    def _commit_intent(
        connection: duckdb.DuckDBPyConnection,
        operation_id: str,
        *,
        stage: str,
        result: Mapping[str, object],
    ) -> None:
        connection.execute(
            """
            UPDATE project_operation_intent
               SET state = 'COMMITTED', stage = ?, result_json = ?,
                   last_error = '', updated_at = ?
             WHERE operation_id = ? AND state = 'PENDING'
            """,
            [stage, canonical_json(result), utc_now().isoformat(), operation_id],
        )

    @staticmethod
    def _set_pending_stage(
        connection: duckdb.DuckDBPyConnection,
        operation_id: str,
        stage: str,
    ) -> None:
        connection.execute(
            """
            UPDATE project_operation_intent
               SET stage = ?, updated_at = ?
             WHERE operation_id = ? AND state = 'PENDING'
            """,
            [stage, utc_now().isoformat(), operation_id],
        )
