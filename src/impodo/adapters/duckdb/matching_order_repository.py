"""Persist the versioned Stage 3 table-order display preference.

Layer: adapter. The preference is workspace-local presentation state. Saving
or resetting it changes only its singleton row and an audit event; it never
invalidates mapping, Recipe, preparation, preflight, or execution evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Protocol

from impodo.domain.matching_order import (
    MatchingOrderCheck,
    MatchingOrderCheckAttempt,
    MatchingOrderCheckPhase,
    MatchingOrderCheckStatus,
    MatchingOrderPreference,
    MatchingOrderVersionConflict,
)
from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.contracts import (
    MappingWorkingDraft,
    OdooSchemaCatalog,
    SourceSelection,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.workbench import WorkspaceStateNotFoundError

from .repository import DuckDbRepository


class MatchingOrderSourceProjection(Protocol):
    """Read the exact effective dataset selection shown by Stage 3."""

    def get_mapping_source_selection(
        self,
        workspace_id: str,
    ) -> SourceSelection | None: ...


class MatchingOrderRepository(DuckDbRepository):
    """Own the current optimistic matching-order preference."""

    def __init__(
        self,
        database,
        source_projection: MatchingOrderSourceProjection | None = None,
    ) -> None:
        super().__init__(database)
        self._source_projection = source_projection

    def get_preference(
        self,
        workspace_id: str,
    ) -> MatchingOrderPreference | None:
        value = self._read_singleton_json(
            workspace_id,
            """
            SELECT preference_json
              FROM matching_order_preference
             WHERE singleton_id = 1
            """,
        )
        return MatchingOrderPreference.from_json(value) if value else None

    def save_preference(
        self,
        workspace_id: str,
        preference: MatchingOrderPreference,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None:
        """Replace the current preference at its exact optimistic version."""

        self._assert_workspace_mutable(workspace_id)
        if preference.workspace_id != workspace_id:
            raise WorkspaceError(
                "Matching-order preference belongs to another workspace"
            )
        if (
            preference.actor_issuer != actor.identity.issuer
            or preference.actor_subject != actor.identity.subject_id
        ):
            raise WorkspaceError("Matching-order actor identity is invalid")
        projected_selection = None
        if self._source_projection is not None:
            projected_selection = (
                self._source_projection.get_mapping_source_selection(workspace_id)
            )
            if projected_selection is None:
                raise WorkspaceError(
                    "Freeze source datasets before saving a table order"
                )
        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                selection = projected_selection
                if selection is None:
                    selection_row = connection.execute(
                        "SELECT selection_json FROM source_selection "
                        "WHERE singleton_id = 1"
                    ).fetchone()
                    if selection_row is None:
                        raise WorkspaceError(
                            "Freeze source datasets before saving a table order"
                        )
                    selection = SourceSelection.from_json(str(selection_row[0]))
                if preference.source_selection_hash != selection.content_hash:
                    raise WorkspaceError(
                        "The source tables changed before this order was saved"
                    )
                current_dataset_ids = tuple(
                    item.dataset_id for item in selection.datasets
                )
                if (
                    len(preference.ordered_dataset_ids)
                    != len(current_dataset_ids)
                    or set(preference.ordered_dataset_ids)
                    != set(current_dataset_ids)
                ):
                    raise WorkspaceError(
                        "Table order must contain every current source table "
                        "exactly once"
                    )
                current = connection.execute(
                    "SELECT version FROM matching_order_preference "
                    "WHERE singleton_id = 1"
                ).fetchone()
                actual_version = int(current[0]) if current is not None else None
                if actual_version != expected_version:
                    raise MatchingOrderVersionConflict(
                        submitted_version=expected_version,
                        current_version=actual_version,
                    )
                if preference.version != (actual_version or 0) + 1:
                    raise WorkspaceError(
                        "Matching-order preference version is not consecutive"
                    )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO matching_order_preference
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        preference.version,
                        preference.source_selection_hash,
                        preference.updated_at.isoformat(),
                        preference.actor_issuer,
                        preference.actor_subject,
                        preference.actor_display_name,
                        preference.to_json(),
                    ],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="MATCHING_ORDER_PREFERENCE_SAVED",
                    detail=(
                        f"version {preference.version}: "
                        f"{len(preference.ordered_dataset_ids)} table(s)"
                    ),
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def reset_preference(
        self,
        workspace_id: str,
        *,
        expected_version: int | None,
        actor: Actor,
    ) -> None:
        """Delete the current preference at its exact optimistic version."""

        self._assert_workspace_mutable(workspace_id)
        database_path = (
            self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        )
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                current = connection.execute(
                    "SELECT version FROM matching_order_preference "
                    "WHERE singleton_id = 1"
                ).fetchone()
                actual_version = int(current[0]) if current is not None else None
                if actual_version != expected_version:
                    raise MatchingOrderVersionConflict(
                        submitted_version=expected_version,
                        current_version=actual_version,
                    )
                if current is None:
                    connection.rollback()
                    return
                connection.execute(
                    "DELETE FROM matching_order_preference WHERE singleton_id = 1"
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="MATCHING_ORDER_PREFERENCE_RESET",
                    detail=f"reset version {actual_version}",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def begin_check(
        self,
        workspace_id: str,
        attempt: MatchingOrderCheckAttempt,
        *,
        actor: Actor,
    ) -> tuple[MatchingOrderCheckAttempt, bool]:
        """Persist one active attempt or return the workspace's existing one."""

        self._assert_workspace_mutable(workspace_id)
        if attempt.workspace_id != workspace_id:
            raise WorkspaceError("Matching-order check belongs to another workspace")
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                active = connection.execute(
                    """
                    SELECT attempt.*
                      FROM matching_order_check_active AS active
                      JOIN matching_order_check_attempt AS attempt
                        ON attempt.check_id = active.check_id
                     WHERE active.singleton_id = 1
                    """
                ).fetchone()
                if active is not None:
                    existing = _attempt_from_row(workspace_id, active)
                    if existing.active:
                        connection.rollback()
                        return existing, False
                    connection.execute(
                        "DELETE FROM matching_order_check_active WHERE singleton_id = 1"
                    )
                connection.execute(
                    "INSERT INTO matching_order_check_attempt VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _attempt_values(attempt),
                )
                connection.execute(
                    "INSERT INTO matching_order_check_active VALUES (1, ?)",
                    [attempt.check_id],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="MATCHING_ORDER_CHECK_STARTED",
                    detail=f"check {attempt.check_id}",
                    actor=actor,
                )
                connection.commit()
                return attempt, True
            except Exception:
                connection.rollback()
                raise

    def update_check_attempt(
        self,
        workspace_id: str,
        attempt: MatchingOrderCheckAttempt,
    ) -> None:
        """Advance only the currently active bounded status row."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            active = connection.execute(
                "SELECT check_id FROM matching_order_check_active WHERE singleton_id = 1"
            ).fetchone()
            if active is None or str(active[0]) != attempt.check_id:
                raise WorkspaceError("Matching-order check is no longer active")
            connection.execute(
                """
                UPDATE matching_order_check_attempt
                   SET status = ?, phase = ?, message = ?, progress_percent = ?,
                       failure_message = ?, updated_at = ?, finished_at = ?
                 WHERE check_id = ?
                """,
                [
                    attempt.status.value,
                    attempt.phase.value,
                    attempt.message,
                    attempt.progress_percent,
                    attempt.failure_message,
                    attempt.updated_at.isoformat(),
                    attempt.finished_at.isoformat() if attempt.finished_at else None,
                    attempt.check_id,
                ],
            )

    def publish_check(
        self,
        workspace_id: str,
        check: MatchingOrderCheck,
        *,
        protected_snapshot_json: str,
        actor: Actor,
    ) -> MatchingOrderCheckStatus:
        """Atomically store protected evidence and replace a compatible pointer."""

        self._assert_workspace_mutable(workspace_id)
        if check.workspace_id != workspace_id:
            raise WorkspaceError("Matching-order check belongs to another workspace")
        try:
            protected_payload = json.loads(protected_snapshot_json)
        except json.JSONDecodeError as error:
            raise WorkspaceError("Matching-order protected evidence is invalid") from error
        snapshot_hash = content_hash(protected_payload)
        projected_selection = (
            self._source_projection.get_mapping_source_selection(workspace_id)
            if self._source_projection is not None
            else None
        )
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                active = connection.execute(
                    "SELECT check_id FROM matching_order_check_active WHERE singleton_id = 1"
                ).fetchone()
                if active is None or str(active[0]) != check.check_id:
                    raise WorkspaceError("Matching-order check is no longer active")
                draft_row = connection.execute(
                    "SELECT draft_json FROM mapping_working_draft WHERE singleton_id = 1"
                ).fetchone()
                schema_row = connection.execute(
                    "SELECT catalog_json FROM odoo_schema_catalog WHERE singleton_id = 1"
                ).fetchone()
                governance_row = connection.execute(
                    """
                    SELECT revision.governance_json
                      FROM schema_governance_current AS current
                      JOIN schema_governance_revision AS revision
                        ON revision.governance_id = current.governance_id
                       AND revision.version = current.version
                     WHERE current.singleton_id = 1
                    """
                ).fetchone()
                draft = (
                    MappingWorkingDraft.from_json(str(draft_row[0]))
                    if draft_row is not None
                    else None
                )
                schema = (
                    OdooSchemaCatalog.from_json(str(schema_row[0]))
                    if schema_row is not None
                    else None
                )
                governance = (
                    SchemaGovernance.from_json(str(governance_row[0]))
                    if governance_row is not None
                    else None
                )
                selection_hash = (
                    projected_selection.content_hash
                    if projected_selection is not None
                    else ""
                )
                governance_hash = (
                    governance.content_hash
                    if governance is not None
                    else (schema.content_hash if schema is not None else "")
                )
                compatible = bool(
                    draft is not None
                    and schema is not None
                    and selection_hash == check.source_selection_hash
                    and schema.content_hash == check.schema_hash
                    and governance_hash == check.governance_hash
                    and draft.version == check.working_draft_version
                    and draft.content_hash == check.working_draft_hash
                    and schema.connection_target_hash == check.target_hash
                    and schema.read_credential_binding_hash
                    == check.read_credential_binding_hash
                    and schema.read_principal_hash == check.read_principal_hash
                    and schema.read_permission_hash == check.read_permission_hash
                    and schema.read_context_hash == check.read_context_hash
                )
                connection.execute(
                    "INSERT INTO matching_order_check VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        check.check_id,
                        check.captured_at.isoformat(),
                        check.source_selection_hash,
                        check.schema_hash,
                        check.governance_hash,
                        check.working_draft_version,
                        check.working_draft_hash,
                        check.target_hash,
                        check.read_credential_binding_hash,
                        check.read_principal_hash,
                        check.read_permission_hash,
                        check.read_context_hash,
                        check.recommendation_hash,
                        check.to_json(),
                    ],
                )
                connection.execute(
                    "INSERT INTO matching_order_protected_snapshot VALUES (?, ?, ?)",
                    [check.check_id, snapshot_hash, protected_snapshot_json],
                )
                status = (
                    MatchingOrderCheckStatus.SUCCEEDED
                    if compatible
                    else MatchingOrderCheckStatus.STALE
                )
                if compatible:
                    connection.execute(
                        "INSERT OR REPLACE INTO matching_order_check_current VALUES (1, ?)",
                        [check.check_id],
                    )
                now = datetime.now(timezone.utc)
                connection.execute(
                    """
                    UPDATE matching_order_check_attempt
                       SET status = ?, phase = 'COMPLETE', message = ?,
                           progress_percent = 100, updated_at = ?, finished_at = ?
                     WHERE check_id = ?
                    """,
                    [
                        status.value,
                        (
                            "Odoo suggestion updated"
                            if compatible
                            else "Check finished after the saved matching data changed"
                        ),
                        now.isoformat(),
                        now.isoformat(),
                        check.check_id,
                    ],
                )
                connection.execute(
                    "DELETE FROM matching_order_check_active WHERE singleton_id = 1"
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type=(
                        "MATCHING_ORDER_CHECK_PUBLISHED"
                        if compatible
                        else "MATCHING_ORDER_CHECK_STALE"
                    ),
                    detail=f"check {check.check_id}: {check.recommendation_hash}",
                    actor=actor,
                )
                connection.commit()
                return status
            except Exception:
                connection.rollback()
                raise

    def fail_check(
        self,
        workspace_id: str,
        attempt: MatchingOrderCheckAttempt,
        *,
        actor: Actor,
    ) -> None:
        """Record a safe failed attempt while preserving the current result."""

        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            connection.begin()
            try:
                connection.execute(
                    """
                    UPDATE matching_order_check_attempt
                       SET status = ?, phase = ?, message = ?, progress_percent = ?,
                           failure_message = ?, updated_at = ?, finished_at = ?
                     WHERE check_id = ?
                    """,
                    [
                        attempt.status.value,
                        attempt.phase.value,
                        attempt.message,
                        attempt.progress_percent,
                        attempt.failure_message,
                        attempt.updated_at.isoformat(),
                        attempt.finished_at.isoformat() if attempt.finished_at else None,
                        attempt.check_id,
                    ],
                )
                connection.execute(
                    "DELETE FROM matching_order_check_active WHERE check_id = ?",
                    [attempt.check_id],
                )
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="MATCHING_ORDER_CHECK_FAILED",
                    detail=f"check {attempt.check_id}: {attempt.failure_message[:300]}",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_check_attempt(
        self,
        workspace_id: str,
        check_id: str,
    ) -> MatchingOrderCheckAttempt | None:
        rows = self._read_attempt_rows(
            workspace_id,
            "SELECT check_id, status, phase, message, progress_percent, "
            "failure_message, created_at, updated_at, finished_at "
            "FROM matching_order_check_attempt WHERE check_id = ?",
            [check_id],
        )
        return _attempt_from_row(workspace_id, rows[0]) if rows else None

    def get_active_check_attempt(
        self,
        workspace_id: str,
    ) -> MatchingOrderCheckAttempt | None:
        rows = self._read_attempt_rows(
            workspace_id,
            """
            SELECT attempt.check_id, attempt.status, attempt.phase,
                   attempt.message, attempt.progress_percent,
                   attempt.failure_message, attempt.created_at,
                   attempt.updated_at, attempt.finished_at
              FROM matching_order_check_active AS active
              JOIN matching_order_check_attempt AS attempt
                ON attempt.check_id = active.check_id
             WHERE active.singleton_id = 1
            """,
            None,
        )
        return _attempt_from_row(workspace_id, rows[0]) if rows else None

    def get_current_check(self, workspace_id: str) -> MatchingOrderCheck | None:
        values = self._read_json_rows(
            workspace_id,
            """
            SELECT result.check_json
              FROM matching_order_check_current AS current
              JOIN matching_order_check AS result
                ON result.check_id = current.check_id
             WHERE current.singleton_id = 1
            """,
        )
        return MatchingOrderCheck.from_json(values[0]) if values else None

    def _read_attempt_rows(
        self,
        workspace_id: str,
        query: str,
        parameters: list[object] | None,
    ):
        database_path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        if not database_path.is_file():
            raise WorkspaceStateNotFoundError("Workspace engine state not found")
        with self._connect(database_path) as connection:
            self._ensure_workspace_database_schema(connection)
            return tuple(connection.execute(query, parameters or []).fetchall())


def _attempt_values(attempt: MatchingOrderCheckAttempt) -> list[object]:
    return [
        attempt.check_id,
        attempt.status.value,
        attempt.phase.value,
        attempt.message,
        attempt.progress_percent,
        attempt.failure_message,
        attempt.created_at.isoformat(),
        attempt.updated_at.isoformat(),
        attempt.finished_at.isoformat() if attempt.finished_at else None,
    ]


def _attempt_from_row(
    workspace_id: str,
    row,
) -> MatchingOrderCheckAttempt:
    return MatchingOrderCheckAttempt(
        check_id=str(row[0]),
        workspace_id=workspace_id,
        status=MatchingOrderCheckStatus(str(row[1])),
        phase=MatchingOrderCheckPhase(str(row[2])),
        message=str(row[3]),
        progress_percent=int(row[4]),
        failure_message=str(row[5]),
        created_at=datetime.fromisoformat(str(row[6])),
        updated_at=datetime.fromisoformat(str(row[7])),
        finished_at=(datetime.fromisoformat(str(row[8])) if row[8] else None),
    )
