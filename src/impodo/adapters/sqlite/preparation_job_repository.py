"""SQLite persistence for durable background-preparation job state."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator
from uuid import uuid4

from ...access import ActorIdentity
from ...preparation_jobs import (
    PHASE_LABELS,
    PreparationJob,
    PreparationJobStatus,
    PreparationPhase,
    preparation_progress_percent,
)


class PreparationJobNotFoundError(LookupError):
    """Raised when a job ID is absent or belongs to another project."""


class PreparationJobStateError(ValueError):
    """Raised when a requested job transition is not allowed."""


class PreparationJobRepository:
    """Persist the small job ledger separately from project data writes."""

    def __init__(self, root: str | Path, *, recover_interrupted: bool = True) -> None:
        resolved_root = Path(root).resolve()
        resolved_root.mkdir(parents=True, exist_ok=True)
        self.path = resolved_root / "preparation-jobs.sqlite3"
        self._initialize()
        if recover_interrupted:
            self.recover_interrupted()

    def enqueue(
        self,
        project_id: str,
        project_name: str,
        total_rows: int,
        requested_by: ActorIdentity,
    ) -> tuple[PreparationJob, bool]:
        """Create one attempt or return the project's already-active attempt."""

        now = _now_text()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT * FROM preparation_job
                 WHERE project_id = ? AND status IN ('QUEUED', 'RUNNING')
                 ORDER BY created_at DESC LIMIT 1
                """,
                [project_id],
            ).fetchone()
            if active is not None:
                connection.commit()
                return _job_from_row(active), False
            attempt_row = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 FROM preparation_job WHERE project_id = ?",
                [project_id],
            ).fetchone()
            attempt = int(attempt_row[0])
            job_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO preparation_job (
                    job_id, project_id, project_name, status, phase, message,
                    completed_rows, total_rows, progress_percent, attempt,
                    cancel_requested, requested_by_issuer, requested_by_subject,
                    requested_by_display_name, created_at, started_at,
                    updated_at, finished_at, result_run_id, failure_code,
                    failure_message
                ) VALUES (?, ?, ?, 'QUEUED', 'QUEUED', ?, 0, ?, 0, ?, 0,
                          ?, ?, ?, ?, NULL, ?, NULL, '', '', '')
                """,
                [
                    job_id,
                    project_id,
                    project_name.strip()[:300] or "Data preparation project",
                    PHASE_LABELS[PreparationPhase.QUEUED],
                    max(0, int(total_rows)),
                    attempt,
                    requested_by.issuer,
                    requested_by.subject_id,
                    requested_by.display_name,
                    now,
                    now,
                ],
            )
            row = connection.execute(
                "SELECT * FROM preparation_job WHERE job_id = ?", [job_id]
            ).fetchone()
            connection.commit()
        assert row is not None
        return _job_from_row(row), True

    def get(self, project_id: str, job_id: str) -> PreparationJob:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM preparation_job WHERE project_id = ? AND job_id = ?",
                [project_id, job_id],
            ).fetchone()
        if row is None:
            raise PreparationJobNotFoundError("Preparation job not found")
        return _job_from_row(row)

    def get_by_id(self, job_id: str) -> PreparationJob:
        """Return one job when the supervising process already owns its ID."""

        return self._get_by_id(job_id)

    def active(self, project_id: str) -> PreparationJob | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM preparation_job
                 WHERE project_id = ? AND status IN ('QUEUED', 'RUNNING')
                 ORDER BY created_at DESC LIMIT 1
                """,
                [project_id],
            ).fetchone()
        return _job_from_row(row) if row is not None else None

    def delete_project_history(self, project_id: str) -> None:
        """Remove terminal control-plane history after project deletion."""

        with self._connection() as connection:
            active = connection.execute(
                """
                SELECT 1 FROM preparation_job
                 WHERE project_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                [project_id],
            ).fetchone()
            if active is not None:
                raise PreparationJobStateError(
                    "Stop the active preparation before deleting this project"
                )
            connection.execute(
                "DELETE FROM preparation_job WHERE project_id = ?", [project_id]
            )
            connection.commit()

    def mark_running(self, job_id: str) -> PreparationJob:
        now = _now_text()
        self._transition(
            job_id,
            """
            UPDATE preparation_job
               SET status = 'RUNNING', phase = 'VALIDATING',
                   message = ?, progress_percent = 3,
                   started_at = COALESCE(started_at, ?), updated_at = ?
             WHERE job_id = ? AND status = 'QUEUED'
            """,
            [PHASE_LABELS[PreparationPhase.VALIDATING], now, now, job_id],
            allowed_noop=PreparationJobStatus.RUNNING,
        )
        return self._get_by_id(job_id)

    def update_progress(
        self,
        job_id: str,
        phase: PreparationPhase,
        *,
        completed_rows: int,
        total_rows: int,
        message: str = "",
    ) -> PreparationJob:
        percent = preparation_progress_percent(
            phase,
            completed_rows=completed_rows,
            total_rows=total_rows,
        )
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE preparation_job
                   SET phase = ?, message = ?,
                       completed_rows = MAX(completed_rows, ?),
                       total_rows = MAX(total_rows, ?),
                       progress_percent = MAX(progress_percent, ?),
                       updated_at = ?
                 WHERE job_id = ? AND status = 'RUNNING'
                """,
                [
                    phase.value,
                    (message.strip()[:500] or PHASE_LABELS[phase]),
                    max(0, int(completed_rows)),
                    max(0, int(total_rows)),
                    percent,
                    _now_text(),
                    job_id,
                ],
            )
            connection.commit()
        if cursor.rowcount == 0:
            current = self._get_by_id(job_id)
            if not current.terminal:
                raise PreparationJobStateError("Preparation job is not running")
        return self._get_by_id(job_id)

    def request_cancel(self, project_id: str, job_id: str) -> PreparationJob:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE preparation_job
                   SET cancel_requested = 1,
                       message = 'Stopping safely after the current batch',
                       updated_at = ?
                 WHERE project_id = ? AND job_id = ?
                   AND status IN ('QUEUED', 'RUNNING')
                """,
                [_now_text(), project_id, job_id],
            )
            connection.commit()
        if cursor.rowcount == 0:
            job = self.get(project_id, job_id)
            if not job.terminal:
                raise PreparationJobStateError("Preparation job cannot be cancelled")
        return self.get(project_id, job_id)

    def mark_succeeded(self, job_id: str, result_run_id: str) -> PreparationJob:
        return self._finish(
            job_id,
            status=PreparationJobStatus.SUCCEEDED,
            phase=PreparationPhase.COMPLETE,
            message="Prepared data is ready for review",
            result_run_id=result_run_id,
        )

    def mark_review_required(self, job_id: str) -> PreparationJob:
        return self._finish(
            job_id,
            status=PreparationJobStatus.REVIEW_REQUIRED,
            phase=PreparationPhase.COMPLETE,
            message="Possible duplicate records need your review",
        )

    def mark_cancelled(self, job_id: str) -> PreparationJob:
        return self._finish(
            job_id,
            status=PreparationJobStatus.CANCELLED,
            phase=PreparationPhase.COMPLETE,
            message="Preparation was stopped safely",
        )

    def mark_failed(
        self,
        job_id: str,
        failure_code: str,
        failure_message: str,
    ) -> PreparationJob:
        return self._finish(
            job_id,
            status=PreparationJobStatus.FAILED,
            phase=PreparationPhase.COMPLETE,
            message="Preparation could not be completed",
            failure_code=failure_code,
            failure_message=failure_message,
        )

    def recover_interrupted(self) -> int:
        """Close jobs whose owning local application is no longer running."""

        now = _now_text()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE preparation_job
                   SET status = 'FAILED', phase = 'COMPLETE',
                       message = 'Preparation stopped before it finished',
                       failure_code = 'WORKER_INTERRUPTED',
                       failure_message = 'Impodo stopped before preparation finished. Try again.',
                       finished_at = ?, updated_at = ?
                 WHERE status IN ('QUEUED', 'RUNNING')
                """,
                [now, now],
            )
            connection.commit()
            return cursor.rowcount

    def _finish(
        self,
        job_id: str,
        *,
        status: PreparationJobStatus,
        phase: PreparationPhase,
        message: str,
        result_run_id: str = "",
        failure_code: str = "",
        failure_message: str = "",
    ) -> PreparationJob:
        now = _now_text()
        percent = 100 if status in {
            PreparationJobStatus.SUCCEEDED,
            PreparationJobStatus.REVIEW_REQUIRED,
        } else None
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE preparation_job
                   SET status = ?, phase = ?, message = ?,
                       progress_percent = COALESCE(?, progress_percent),
                       cancel_requested = CASE
                           WHEN ? = 'CANCELLED' THEN 1 ELSE cancel_requested
                       END,
                       result_run_id = ?, failure_code = ?, failure_message = ?,
                       finished_at = ?, updated_at = ?
                 WHERE job_id = ? AND status IN ('QUEUED', 'RUNNING')
                """,
                [
                    status.value,
                    phase.value,
                    message[:500],
                    percent,
                    status.value,
                    result_run_id[:200],
                    failure_code.strip()[:200],
                    failure_message.strip()[:1000],
                    now,
                    now,
                    job_id,
                ],
            )
            connection.commit()
        if cursor.rowcount == 0:
            current = self._get_by_id(job_id)
            if current.status is not status:
                raise PreparationJobStateError("Preparation job is already complete")
        return self._get_by_id(job_id)

    def _transition(
        self,
        job_id: str,
        query: str,
        parameters: list[object],
        *,
        allowed_noop: PreparationJobStatus,
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(query, parameters)
            connection.commit()
        if cursor.rowcount == 0 and self._get_by_id(job_id).status is not allowed_noop:
            raise PreparationJobStateError("Preparation job transition is not allowed")

    def _get_by_id(self, job_id: str) -> PreparationJob:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM preparation_job WHERE job_id = ?", [job_id]
            ).fetchone()
        if row is None:
            raise PreparationJobNotFoundError("Preparation job not found")
        return _job_from_row(row)

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS preparation_job (
                    job_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    project_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    message TEXT NOT NULL,
                    completed_rows INTEGER NOT NULL,
                    total_rows INTEGER NOT NULL,
                    progress_percent INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL,
                    requested_by_issuer TEXT NOT NULL,
                    requested_by_subject TEXT NOT NULL,
                    requested_by_display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    result_run_id TEXT NOT NULL,
                    failure_code TEXT NOT NULL,
                    failure_message TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS preparation_job_one_active
                    ON preparation_job (project_id)
                    WHERE status IN ('QUEUED', 'RUNNING');
                CREATE INDEX IF NOT EXISTS preparation_job_project_history
                    ON preparation_job (project_id, created_at DESC);
                """
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        try:
            yield connection
        finally:
            connection.close()


def _job_from_row(row: sqlite3.Row) -> PreparationJob:
    return PreparationJob(
        job_id=str(row["job_id"]),
        project_id=str(row["project_id"]),
        project_name=str(row["project_name"]),
        status=PreparationJobStatus(str(row["status"])),
        phase=PreparationPhase(str(row["phase"])),
        message=str(row["message"]),
        completed_rows=int(row["completed_rows"]),
        total_rows=int(row["total_rows"]),
        progress_percent=int(row["progress_percent"]),
        attempt=int(row["attempt"]),
        cancel_requested=bool(row["cancel_requested"]),
        requested_by_issuer=str(row["requested_by_issuer"]),
        requested_by_subject=str(row["requested_by_subject"]),
        requested_by_display_name=str(row["requested_by_display_name"]),
        created_at=_datetime(str(row["created_at"])),
        started_at=_optional_datetime(row["started_at"]),
        updated_at=_datetime(str(row["updated_at"])),
        finished_at=_optional_datetime(row["finished_at"]),
        result_run_id=str(row["result_run_id"]),
        failure_code=str(row["failure_code"]),
        failure_message=str(row["failure_message"]),
    )


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _optional_datetime(value: object) -> datetime | None:
    return _datetime(str(value)) if value else None
