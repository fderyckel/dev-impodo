"""Reserve, advance, and commit restart-safe registry operation intents."""

from __future__ import annotations

from collections.abc import Mapping

import duckdb

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
    required_text,
    utc_now,
)
from impodo.domain.shared.access import Actor

from ...application.preflight_attempts import PreflightComparisonRequest
from ...domain.serialization import canonical_json
from .foundation_repository_mixin import FoundationRepositoryMixin


class FoundationOperationIntents(FoundationRepositoryMixin):
    _PREFLIGHT_OWNER_KIND = "MIGRATION_WORKSPACE"
    _PREFLIGHT_PHASES = frozenset(
        {
            "INTENT_RESERVED",
            "QUEUED",
            "VERIFYING",
            "READING",
            "COMPARING",
            "BUILDING",
            "PUBLISHING",
            "COMPLETE",
            "INTERRUPTED",
            "STALE",
            "FAILED",
        }
    )

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

    def reserve_preflight_comparison(
        self,
        request: PreflightComparisonRequest,
        *,
        actor: Actor,
    ) -> MigrationOperationIntent:
        """Reserve or replay one exact value-free Final review request."""

        with self.database.connect(self.registry_path) as connection:
            rows = self._rows(
                connection,
                """
                SELECT *
                  FROM project_operation_intent
                 WHERE kind = 'PREFLIGHT_COMPARE'
                   AND owner_kind = ?
                   AND owner_id = ?
                   AND state = 'PENDING'
                 ORDER BY created_at DESC
                """,
                [self._PREFLIGHT_OWNER_KIND, request.workspace_id],
            )
        pending = tuple(self._intent_from_row(row) for row in rows)
        if pending and all(
            item.operation_id != request.operation_id for item in pending
        ):
            raise MigrationConflictError(
                "Another Final review comparison is already active"
            )
        return self._reserve_intent(
            operation_id=request.operation_id,
            project_id=request.project_id,
            owner_kind=self._PREFLIGHT_OWNER_KIND,
            owner_id=request.workspace_id,
            kind=MigrationOperationKind.PREFLIGHT_COMPARE,
            request_hash=request.semantic_input_hash,
            expected_revision=request.workspace_revision,
            detail={
                "data_version_id": request.data_version_id,
                "migration_run_id": request.migration_run_id,
                "recipe_application_id": request.recipe_application_id,
                "requested_at": request.requested_at.isoformat(),
            },
            actor=actor,
        )

    def get_preflight_comparison(
        self,
        operation_id: str,
        workspace_id: str,
    ) -> MigrationOperationIntent:
        """Load one owner-qualified Final review attempt."""

        workspace_id = require_uuid(workspace_id, "workspace_id")
        intent = self.get_operation_intent(operation_id)
        if (
            intent.kind is not MigrationOperationKind.PREFLIGHT_COMPARE
            or intent.owner_kind != self._PREFLIGHT_OWNER_KIND
            or intent.owner_id != workspace_id
        ):
            raise MigrationOperationReplayError(
                "Operation identity does not belong to this Final review"
            )
        return intent

    def list_pending_preflight_comparisons(
        self,
    ) -> tuple[MigrationOperationIntent, ...]:
        """Return unfinished local comparisons for bounded startup recovery."""

        with self.database.connect(self.registry_path) as connection:
            rows = self._rows(
                connection,
                """
                SELECT *
                  FROM project_operation_intent
                 WHERE kind = 'PREFLIGHT_COMPARE'
                   AND owner_kind = ?
                   AND state = 'PENDING'
                 ORDER BY created_at, operation_id
                """,
                [self._PREFLIGHT_OWNER_KIND],
            )
        return tuple(self._intent_from_row(row) for row in rows)

    def advance_preflight_comparison(
        self,
        operation_id: str,
        workspace_id: str,
        phase: str,
    ) -> MigrationOperationIntent:
        """Persist one coarse phase while the attempt remains pending."""

        current = self.get_preflight_comparison(operation_id, workspace_id)
        phase = required_text(phase, "phase", maximum=80)
        if phase not in self._PREFLIGHT_PHASES:
            raise MigrationConflictError("Final review phase is invalid")
        if current.state is not MigrationOperationState.PENDING:
            return current
        with self.database.connect(self.registry_path) as connection:
            self._set_pending_stage(connection, operation_id, phase)
        return self.get_preflight_comparison(operation_id, workspace_id)

    def commit_preflight_comparison(
        self,
        operation_id: str,
        workspace_id: str,
        *,
        result: Mapping[str, object],
    ) -> MigrationOperationIntent:
        """Commit the exact verified data-plane receipt idempotently."""

        current = self.get_preflight_comparison(operation_id, workspace_id)
        if current.state is MigrationOperationState.COMMITTED:
            if canonical_json(current.result) != canonical_json(result):
                raise MigrationOperationReplayError(
                    "Final review result differs from the committed receipt"
                )
            return current
        if current.state is not MigrationOperationState.PENDING:
            raise MigrationConflictError("Final review attempt cannot commit")
        self._finish_pending_intent(
            operation_id,
            stage="COMPLETE",
            result=result,
        )
        return self.get_preflight_comparison(operation_id, workspace_id)

    def fail_preflight_comparison(
        self,
        operation_id: str,
        workspace_id: str,
        *,
        failure_code: str,
        stage: str = "FAILED",
    ) -> MigrationOperationIntent:
        """Record a safe terminal code without retaining exception text."""

        current = self.get_preflight_comparison(operation_id, workspace_id)
        failure_code = required_text(
            failure_code,
            "failure_code",
            maximum=120,
        )
        stage = required_text(stage, "stage", maximum=80)
        if stage not in self._PREFLIGHT_PHASES:
            raise MigrationConflictError("Final review failure stage is invalid")
        if current.state is not MigrationOperationState.PENDING:
            return current
        with self.database.connect(self.registry_path) as connection:
            connection.execute(
                """
                UPDATE project_operation_intent
                   SET state = 'FAILED', stage = ?, last_error = ?,
                       updated_at = ?
                 WHERE operation_id = ? AND state = 'PENDING'
                """,
                [stage, failure_code, utc_now().isoformat(), operation_id],
            )
        return self.get_preflight_comparison(operation_id, workspace_id)

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
