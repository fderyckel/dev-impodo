"""Persist operator stops and bounded rejection budgets beside load journals.

Every mutation is local and synchronous. No connection survives an Odoo call.
The in-flight transition checks these settings in its own journal transaction.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from threading import RLock

import duckdb

from impodo.domain.execution.load_control import (
    LoadControl,
    can_set_aside,
)
from impodo.domain.execution.models import ExecutionRowAttempt, ExecutionRowStatus
from impodo.domain.execution.odoo_write import OdooWriteRejected
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.errors import WorkspaceError

from .database import DuckDbWorkspaceDatabase
from .repository import DuckDbRepository


def read_load_control(
    connection: duckdb.DuckDBPyConnection, run_id: str
) -> LoadControl | None:
    """Read one bounded companion; an absent record preserves legacy behavior."""
    row = connection.execute(
        "SELECT control_json FROM execution_load_control WHERE run_id = ?", [run_id]
    ).fetchone()
    return LoadControl.from_json(str(row[0])) if row else None


def write_load_control(
    connection: duckdb.DuckDBPyConnection, run_id: str, control: LoadControl
) -> None:
    """Publish settings only inside the owning journal transaction."""
    payload = control.to_json()
    previous = connection.execute(
        "SELECT control_json FROM execution_load_control WHERE run_id = ?", [run_id]
    ).fetchone()
    if previous is not None and str(previous[0]) == payload:
        return
    connection.execute(
        "INSERT INTO execution_load_control_event SELECT ?, COALESCE(MAX(ordinal), -1) + 1, ? FROM execution_load_control_event WHERE run_id = ?",
        [run_id, payload, run_id],
    )
    connection.execute(
        "INSERT OR REPLACE INTO execution_load_control VALUES (?, ?)", [run_id, payload]
    )


class ExecutionLoadControlRecords(DuckDbRepository):
    """Share the journal database owner without caching a physical connection."""

    def __init__(self, database: DuckDbWorkspaceDatabase) -> None:
        super().__init__(database)
        # The application's single journal owner serializes short local control
        # transactions. A stop and the next in-flight transition therefore have
        # one order, even when DuckDB snapshots would otherwise allow write skew.
        # This lock is never retained during transport or remote read-back.
        self._load_control_lock = RLock()

    def _ensure_load_control_schema(
        self, path: Path, connection: duckdb.DuckDBPyConnection
    ) -> None:
        self._ensure_workspace_database_schema(connection)

    @staticmethod
    def _require_mutable_control(connection: duckdb.DuckDBPyConnection) -> None:
        state = connection.execute(
            "SELECT status FROM workspace_projection_cache WHERE singleton_id = 1"
        ).fetchone()
        if state is None or state[0] == "CLOSED":
            raise WorkspaceError("This workspace is unavailable or read-only")

    def get_load_control(self, workspace_id: str, run_id: str) -> LoadControl | None:
        """Return the exact durable policy and stopping state for one load."""
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._load_control_lock, self._connect(path) as connection:
            self._ensure_load_control_schema(path, connection)
            return read_load_control(connection, run_id)

    def request_load_stop(
        self, workspace_id: str, run_id: str, *, actor: Actor
    ) -> LoadControl:
        """Prevent the next transport atomically without interrupting this one."""
        return self._change_control(
            workspace_id, run_id, actor=actor, expected_hash=None
        )

    def resume_load_control(
        self, workspace_id: str, run_id: str, *, expected_hash: str, actor: Actor
    ) -> LoadControl:
        """Approve another bounded probe after the caller's recovery checks."""
        return self._change_control(
            workspace_id, run_id, actor=actor, expected_hash=expected_hash
        )

    def _change_control(
        self, workspace_id: str, run_id: str, *, actor: Actor, expected_hash: str | None
    ) -> LoadControl:
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._load_control_lock, self._connect(path) as connection:
            self._ensure_load_control_schema(path, connection)
            connection.begin()
            try:
                self._require_mutable_control(connection)
                current = connection.execute(
                    "SELECT run.status FROM execution_run AS run JOIN execution_current AS active USING(run_id) WHERE run.run_id = ?",
                    [run_id],
                ).fetchone()
                previous = read_load_control(connection, run_id) or LoadControl()
                if (
                    current is not None
                    and current[0] != "RUNNING"
                    and expected_hash is None
                    and previous.state.value in {"STOPPED", "FINISHED"}
                ):
                    connection.commit()
                    return previous
                if current is None or current[0] != "RUNNING":
                    raise WorkspaceError(
                        "The active load changed; review its saved result"
                    )
                if expected_hash is None:
                    result = previous.request_stop()
                else:
                    if not expected_hash or previous.content_hash != expected_hash:
                        raise WorkspaceError(
                            "The paused load changed; check recovery again"
                        )
                    result = previous.continue_next_group()
                write_load_control(connection, run_id, result)
                self._insert_workspace_audit(
                    connection,
                    revision=self._workspace_revision(connection),
                    event_type="ODOO_LOAD_STOP_REQUESTED"
                    if expected_hash is None
                    else "ODOO_LOAD_CONTINUED",
                    detail=f"run {run_id}; previous control {previous.content_hash}",
                    actor=actor,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return result

    def record_rejected_batch(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
        error: OdooWriteRejected,
    ) -> tuple[ExecutionRowAttempt, ...]:
        """Retain known rollback proof before making innocent peers retry-ready."""
        if (
            not 2 <= len(rows) <= 1_000
            or len({row.row_id for row in rows}) != len(rows)
            or not can_set_aside(error)
        ):
            raise WorkspaceError("This rejection cannot authorize batch isolation")
        if (
            error.diagnostic is None
            or error.diagnostic.batch_size != len(rows)
            or {row.target_model for row in rows} != {error.diagnostic.model}
        ):
            raise WorkspaceError("The rejection does not describe this exact transport")
        path = self.workspace_directory(workspace_id) / "workspace-engine.duckdb"
        with self._load_control_lock, self._connect(path) as connection:
            self._ensure_load_control_schema(path, connection)
            connection.begin()
            try:
                self._require_mutable_control(connection)
                status = connection.execute(
                    "SELECT status FROM execution_run WHERE run_id = ?", [run_id]
                ).fetchone()
                control = read_load_control(connection, run_id)
                if (
                    status is None
                    or status[0] != "RUNNING"
                    or control is None
                    or control.policy.value != "CONTINUE"
                ):
                    raise WorkspaceError("This load does not allow batch isolation")
                batch_ids = {row.transport_batch for row in rows}
                if len(batch_ids) != 1 or any(
                    row.status is not ExecutionRowStatus.IN_FLIGHT
                    or row.transport_phase != "CREATE"
                    for row in rows
                ):
                    raise WorkspaceError("Rejected transport identities changed")
                batch = next(iter(batch_ids))
                stored = connection.execute(
                    "SELECT row_json FROM execution_row WHERE run_id = ? AND status = 'IN_FLIGHT' AND json_extract(row_json, '$.transport_batch')::BIGINT = ? ORDER BY ordinal",
                    [run_id, batch],
                ).fetchall()
                attempts = tuple(
                    ExecutionRowAttempt.from_json(str(item[0])) for item in stored
                )
                if {a.to_json() for a in attempts} != {a.to_json() for a in rows}:
                    raise WorkspaceError(
                        "Rejected transport no longer matches its journal"
                    )
                payload = {
                    "contract_version": 1,
                    "attempts": [json.loads(a.to_json()) for a in attempts],
                    "diagnostic": error.diagnostic.to_dict()
                    if error.diagnostic
                    else None,
                }
                evidence = canonical_json(
                    {**payload, "content_hash": content_hash(payload)}
                )
                connection.execute(
                    "INSERT INTO execution_rejected_transport VALUES (?, ?, ?)",
                    [run_id, batch, evidence],
                )
                result = tuple(
                    replace(
                        a,
                        status=ExecutionRowStatus.RETRY_READY,
                        safe_error="Odoo rolled back this batch; isolation pending",
                    )
                    for a in rows
                )
                for item in result:
                    connection.execute(
                        "UPDATE execution_row SET status = ?, safe_error = ?, row_json = ? WHERE run_id = ? AND row_id = ?",
                        [
                            item.status.value,
                            item.safe_error,
                            item.to_json(),
                            run_id,
                            item.row_id,
                        ],
                    )
                write_load_control(connection, run_id, control.settle_many(()))
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return result
