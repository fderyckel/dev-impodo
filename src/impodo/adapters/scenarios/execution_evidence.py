"""Persist scenario execution and reconciliation evidence durably.

Migration stages: execution and reconciliation. Layer: adapter.

The journal file is created exclusively before the first Odoo transport call.
Every later row transition is atomically replaced. If a process stops with a
current journal, a new execution attempt sees that journal and must use the
normal recovery/read-back path; it cannot silently start from the beginning.

These files are protected run evidence and can contain numeric Odoo receipts.
They are deliberately separate from the compact scenario result.

See ``docs/plans/end-to-end-trial-and-scenario-qualification.md`` and
``tests/integration/scenarios/test_execution_evidence.py``.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Sequence

from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.execution_snapshot import ExecutionSnapshot
from impodo.domain.reconciliation import ReconciliationRun
from impodo.domain.shared.access import Actor
from impodo.domain.shared.models import canonical_json_bytes
from impodo.domain.workspace.errors import WorkspaceError


class ScenarioExecutionJournal:
    """Implement the production execution-journal port in a private run folder."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "execution-journal.json"

    def get_current_run(
        self,
        workspace_id: str,
        snapshot_hash: str | None = None,
    ) -> ExecutionRun | None:
        run = self._read()
        if run is None or run.workspace_id != workspace_id:
            return None
        if snapshot_hash is not None and run.snapshot_hash != snapshot_hash:
            return None
        return run

    def get_run(self, workspace_id: str, run_id: str) -> ExecutionRun | None:
        run = self._read()
        if run is None or (run.workspace_id, run.run_id) != (workspace_id, run_id):
            return None
        return run

    def start_run(
        self,
        workspace_id: str,
        run: ExecutionRun,
        *,
        actor: Actor,
        correction_plan_hash: str = "",
        transfer_preflight_hash: str = "",
    ) -> None:
        del actor, correction_plan_hash, transfer_preflight_hash
        if run.workspace_id != workspace_id or self.path.exists():
            raise WorkspaceError(
                "This scenario target already has execution evidence. "
                "Assess or retain it before another load."
            )
        payload = _execution_run_payload(run)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise WorkspaceError(
                "This scenario target already has execution evidence. "
                "Assess or retain it before another load."
            ) from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(canonical_json_bytes(payload) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise

    def record_outcomes(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None:
        run = self._required(workspace_id, run_id)
        existing = {item.row_id: item for item in run.rows}
        if any(item.row_id not in existing for item in rows):
            raise WorkspaceError("Scenario execution rows no longer match the journal")
        existing.update({item.row_id: item for item in rows})
        self._replace(
            replace(run, rows=tuple(existing[item.row_id] for item in run.rows))
        )

    def record_batch_started(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None:
        self.record_outcomes(workspace_id, run_id, rows)

    def record_recovery(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
        *,
        actor: Actor,
    ) -> None:
        del actor
        self.record_outcomes(workspace_id, run_id, rows)

    def finish_run(
        self,
        workspace_id: str,
        run_id: str,
        status: ExecutionRunStatus,
        *,
        actor: Actor,
    ) -> ExecutionRun:
        del actor
        run = self._required(workspace_id, run_id)
        completed = replace(
            run,
            status=status,
            completed_at=datetime.now(timezone.utc),
        )
        self._replace(completed)
        return completed

    def _required(self, workspace_id: str, run_id: str) -> ExecutionRun:
        run = self.get_run(workspace_id, run_id)
        if run is None:
            raise WorkspaceError("Scenario execution journal is missing or changed")
        return run

    def _read(self) -> ExecutionRun | None:
        if not self.path.is_file():
            return None
        try:
            return _execution_run_from_payload(
                json.loads(self.path.read_text(encoding="utf-8"))
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkspaceError("Scenario execution journal is invalid") from exc

    def _replace(self, run: ExecutionRun) -> None:
        _atomic_write(self.path, canonical_json_bytes(_execution_run_payload(run)))


class ScenarioReconciliationResults:
    """Implement the production reconciliation-result port in the run folder."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "reconciliation.json"

    def get_current(
        self,
        workspace_id: str,
        execution_run_id: str | None = None,
    ) -> ReconciliationRun | None:
        if not self.path.is_file():
            return None
        try:
            report = ReconciliationRun.from_json(
                self.path.read_text(encoding="utf-8")
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkspaceError("Scenario reconciliation evidence is invalid") from exc
        if report.workspace_id != workspace_id:
            return None
        if execution_run_id and report.execution_run_id != execution_run_id:
            return None
        return report

    def publish(
        self,
        workspace_id: str,
        report: ReconciliationRun,
        *,
        actor: Actor,
    ) -> None:
        del actor
        if report.workspace_id != workspace_id or self.path.exists():
            raise WorkspaceError("Scenario reconciliation evidence already exists")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise WorkspaceError(
                "Scenario reconciliation evidence already exists"
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(report.to_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise


def write_scenario_execution_snapshot(
    snapshot: ExecutionSnapshot,
    directory: str | Path,
) -> Path:
    """Persist the exact reviewed write intent before execution starts."""

    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / "execution-snapshot.json"
    _atomic_write(destination, snapshot.to_json().encode("utf-8"))
    reloaded = ExecutionSnapshot.from_json(destination.read_text(encoding="utf-8"))
    if reloaded.semantic_hash != snapshot.semantic_hash:
        raise WorkspaceError("Scenario execution snapshot could not be retained")
    return destination


def _execution_run_payload(run: ExecutionRun) -> dict[str, object]:
    payload = asdict(run)
    payload["status"] = run.status.value
    payload["started_at"] = run.started_at.isoformat()
    payload["completed_at"] = (
        run.completed_at.isoformat() if run.completed_at is not None else None
    )
    payload["rows"] = [json.loads(item.to_json()) for item in run.rows]
    return payload


def _execution_run_from_payload(payload: dict[str, object]) -> ExecutionRun:
    return ExecutionRun(
        run_id=str(payload["run_id"]),
        workspace_id=str(payload["workspace_id"]),
        snapshot_hash=str(payload["snapshot_hash"]),
        snapshot_root_hash=str(payload["snapshot_root_hash"]),
        preflight_run_id=str(payload["preflight_run_id"]),
        target_hash=str(payload["target_hash"]),
        target_database=str(payload["target_database"]),
        batch_rows=(
            int(payload["batch_rows"])
            if payload.get("batch_rows") is not None
            else None
        ),
        status=ExecutionRunStatus(str(payload["status"])),
        started_at=datetime.fromisoformat(str(payload["started_at"])),
        started_by=str(payload["started_by"]),
        completed_at=(
            datetime.fromisoformat(str(payload["completed_at"]))
            if payload.get("completed_at") is not None
            else None
        ),
        rows=tuple(
            ExecutionRowAttempt.from_json(json.dumps(item))
            for item in payload.get("rows", ())
        ),
        write_credential_binding_hash=str(
            payload.get("write_credential_binding_hash", "")
        ),
        write_principal_hash=str(payload.get("write_principal_hash", "")),
        write_permission_hash=str(payload.get("write_permission_hash", "")),
        write_context_hash=str(payload.get("write_context_hash", "")),
    )


def _atomic_write(destination: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
