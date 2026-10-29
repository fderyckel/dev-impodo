"""Run read-only Odoo comparisons in one bounded background worker."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from threading import Condition, RLock, Thread
from uuid import uuid4

from impodo.application.odoo_read_failures import (
    OdooReadFailure,
    OdooReadFailureCode,
    OdooReadWorkflowError,
    classify_odoo_read_failure,
    odoo_read_failure_from_code,
)
from impodo.application.preflight_attempts import (
    PreflightAttemptStore,
    PreflightComparisonReceipt,
    PreflightComparisonRequest,
)
from impodo.application.preflight_timing import timed_preflight_publication
from impodo.application.workspace.access import (
    WorkspaceAccessContext,
    bind_workspace_access_context,
)
from impodo.domain.project.foundation import (
    MigrationIdentifierConfusionError,
    MigrationOperationIntent,
    MigrationOperationReplayError,
    MigrationOperationState,
)
from impodo.domain.shared.access import Actor


class PreflightJobStatus(StrEnum):
    """Lifecycle states exposed by the browser progress endpoint."""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class PreflightPhase(StrEnum):
    """Coarse comparison phases that do not disclose business values."""

    QUEUED = "QUEUED"
    VERIFYING = "VERIFYING"
    READING = "READING"
    COMPARING = "COMPARING"
    BUILDING = "BUILDING"
    PUBLISHING = "PUBLISHING"
    COMPLETE = "COMPLETE"


PREFLIGHT_PHASE_LABELS: dict[PreflightPhase, str] = {
    PreflightPhase.QUEUED: "Waiting to start",
    PreflightPhase.VERIFYING: "Verifying the approved data and Odoo access",
    PreflightPhase.READING: "Reading the records needed from Odoo",
    PreflightPhase.COMPARING: "Comparing the prepared records with Odoo",
    PreflightPhase.BUILDING: "Building the final review",
    PreflightPhase.PUBLISHING: "Saving the new comparison",
    PreflightPhase.COMPLETE: "Final review ready",
}

_PHASE_PERCENT = {
    PreflightPhase.QUEUED: 0,
    PreflightPhase.VERIFYING: 5,
    PreflightPhase.READING: 20,
    PreflightPhase.COMPARING: 55,
    PreflightPhase.BUILDING: 75,
    PreflightPhase.PUBLISHING: 90,
    PreflightPhase.COMPLETE: 100,
}


@dataclass(frozen=True, slots=True)
class PreflightJobResult:
    """Safe terminal information returned by comparison orchestration."""

    preflight_run_id: str
    redirect_url: str
    completion_message: str
    receipt: PreflightComparisonReceipt | None = None


@dataclass(frozen=True, slots=True)
class PreflightJob:
    """One comparison attempt; credentials and compared values are excluded."""

    job_id: str
    access_context: WorkspaceAccessContext
    workspace_id: str
    migration_project_name: str
    status: PreflightJobStatus
    phase: PreflightPhase
    message: str
    progress_percent: int
    attempt: int
    created_at: datetime
    started_at: datetime | None
    updated_at: datetime
    finished_at: datetime | None
    preflight_run_id: str
    redirect_url: str
    completion_message: str
    failure: OdooReadFailure | None

    @property
    def active(self) -> bool:
        return self.status in {PreflightJobStatus.QUEUED, PreflightJobStatus.RUNNING}

    @property
    def terminal(self) -> bool:
        return not self.active


PreflightProgress = Callable[[PreflightPhase], None]
PreflightWork = Callable[[PreflightProgress], PreflightJobResult]


class PreflightJobNotFoundError(LookupError):
    """Raised when a comparison job is missing or belongs elsewhere."""


class PreflightJobStateError(ValueError):
    """Raised when comparison work cannot be accepted."""


class PreflightJobManager:
    """Keep non-secret control state in memory and serialize comparisons."""

    def __init__(self, attempt_store: PreflightAttemptStore | None = None) -> None:
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._jobs: dict[str, PreflightJob] = {}
        self._pending: deque[tuple[str, PreflightWork]] = deque()
        self._scheduled: set[str] = set()
        self._request_hashes: dict[str, str] = {}
        self._attempt_store = attempt_store
        self._stopping = False
        self._worker: Thread | None = None

    def reserve(
        self,
        request: PreflightComparisonRequest,
        migration_project_name: str,
        *,
        access_context: WorkspaceAccessContext,
        actor: Actor,
    ) -> PreflightJob:
        """Reserve one semantic operation before any background work is queued."""

        if (
            access_context.workspace_id != request.workspace_id
            or access_context.project_id != request.project_id
            or access_context.data_version_id != request.data_version_id
            or access_context.migration_run_id != request.migration_run_id
            or access_context.recipe_application_id != request.recipe_application_id
        ):
            raise MigrationIdentifierConfusionError(
                "Comparison request does not match its workspace access context"
            )
        with self._condition:
            if self._stopping:
                raise PreflightJobStateError("Comparison jobs are stopping")
            active = self._active_locked(request.workspace_id)
            if active is not None and active.job_id != request.operation_id:
                raise PreflightJobStateError(
                    "Another Final review comparison is already active"
                )
            if self._attempt_store is not None:
                intent = self._attempt_store.reserve_preflight_comparison(
                    request,
                    actor=actor,
                )
                job = self._job_from_intent(intent, migration_project_name)
            else:
                current_hash = self._request_hashes.get(request.operation_id)
                if (
                    current_hash is not None
                    and current_hash != request.semantic_input_hash
                ):
                    raise MigrationOperationReplayError(
                        "Operation identity was already used with different meaning"
                    )
                self._request_hashes[request.operation_id] = (
                    request.semantic_input_hash
                )
                job = self._jobs.get(request.operation_id) or self._new_job(
                    request.operation_id,
                    request.workspace_id,
                    migration_project_name,
                    access_context,
                )
            self._jobs[job.job_id] = job
            return job

    def enqueue(
        self,
        workspace_id: str,
        migration_project_name: str,
        *,
        access_context: WorkspaceAccessContext,
        work: PreflightWork,
        operation_id: str | None = None,
    ) -> PreflightJob:
        """Create one attempt or return the workspace's active attempt."""

        if access_context.workspace_id != workspace_id:
            raise MigrationIdentifierConfusionError(
                "Comparison access context does not belong to this workspace"
            )
        with self._condition:
            if self._stopping:
                raise PreflightJobStateError("Comparison jobs are stopping")
            active = self._active_locked(workspace_id)
            if active is not None:
                if active.access_context != access_context:
                    raise MigrationIdentifierConfusionError(
                        "Active comparison belongs to another workspace context"
                    )
                if operation_id is None:
                    return active
                if active.job_id != operation_id:
                    raise PreflightJobStateError(
                        "Another Final review comparison is already active"
                    )
            job_id = operation_id or str(uuid4())
            job = active if active is not None else self._jobs.get(job_id)
            if job is None and self._attempt_store is not None and operation_id:
                intent = self._attempt_store.get_preflight_comparison(
                    operation_id,
                    workspace_id,
                )
                job = self._job_from_intent(intent, migration_project_name)
            if job is None:
                job = self._new_job(
                    job_id,
                    workspace_id,
                    migration_project_name,
                    access_context,
                )
            if job.access_context != access_context:
                raise MigrationIdentifierConfusionError(
                    "Reserved comparison belongs to another workspace context"
                )
            if job.terminal or job.job_id in self._scheduled:
                return job
            self._jobs[job.job_id] = job
            self._pending.append((job.job_id, work))
            self._scheduled.add(job.job_id)
            self._persist_phase(job, PreflightPhase.QUEUED)
            if self._worker is None:
                self._worker = Thread(
                    target=self._work,
                    name="impodo-preflight",
                    daemon=True,
                )
                self._worker.start()
            self._condition.notify()
            return job

    def get(self, workspace_id: str, job_id: str) -> PreflightJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None and self._attempt_store is not None:
                try:
                    intent = self._attempt_store.get_preflight_comparison(
                        job_id,
                        workspace_id,
                    )
                except (MigrationIdentifierConfusionError, LookupError, ValueError):
                    intent = None
                if intent is not None:
                    job = self._job_from_intent(intent, "Data project")
                    self._jobs[job.job_id] = job
            if job is None or job.workspace_id != workspace_id:
                raise PreflightJobNotFoundError("Comparison job not found")
            return job

    def active(self, workspace_id: str) -> PreflightJob | None:
        with self._lock:
            active = self._active_locked(workspace_id)
            if active is not None or self._attempt_store is None:
                return active
            for intent in self._attempt_store.list_pending_preflight_comparisons():
                if intent.owner_id == workspace_id:
                    job = self._job_from_intent(intent, "Data project")
                    self._jobs[job.job_id] = job
                    return job
            return None

    def reconcile_pending(
        self,
        resolver: Callable[
            [str, str],
            PreflightComparisonReceipt | None,
        ],
    ) -> None:
        """Resolve unfinished registry attempts from committed Workspace evidence."""

        if self._attempt_store is None:
            return
        for intent in self._attempt_store.list_pending_preflight_comparisons():
            try:
                receipt = resolver(intent.owner_id, intent.operation_id)
            except Exception as error:  # noqa: BLE001 - startup recovery boundary
                failure = classify_odoo_read_failure(error)
                stage = (
                    "STALE"
                    if failure.code
                    in {
                        OdooReadFailureCode.MAPPING_EVIDENCE_STALE,
                        OdooReadFailureCode.PREPARED_EVIDENCE_STALE,
                        OdooReadFailureCode.SCHEMA_EVIDENCE_STALE,
                    }
                    else "FAILED"
                )
                self._attempt_store.fail_preflight_comparison(
                    intent.operation_id,
                    intent.owner_id,
                    failure_code=failure.code.value,
                    stage=stage,
                )
            else:
                if receipt is None:
                    self._attempt_store.fail_preflight_comparison(
                        intent.operation_id,
                        intent.owner_id,
                        failure_code=(
                            OdooReadFailureCode.COMPARISON_INTERRUPTED.value
                        ),
                        stage="INTERRUPTED",
                    )
                else:
                    self._attempt_store.commit_preflight_comparison(
                        intent.operation_id,
                        intent.owner_id,
                        result={
                            **receipt.to_mapping(),
                            "completion_message": (
                                "The saved Final review was recovered after restart."
                            ),
                            "redirect_url": (
                                f"/workspaces/{intent.owner_id}/summary"
                            ),
                        },
                    )

    def shutdown(self) -> None:
        """Stop accepting work without interrupting an in-flight Odoo read."""

        with self._condition:
            self._stopping = True
            pending_ids = tuple(job_id for job_id, _work in self._pending)
            self._pending.clear()
            for job_id in pending_ids:
                self._finish_failed(
                    job_id,
                    OdooReadWorkflowError(
                        OdooReadFailureCode.COMPARISON_INTERRUPTED,
                        "Impodo stopped before the comparison started",
                    ),
                )
            self._condition.notify_all()
        if self._worker is not None:
            self._worker.join(timeout=0.25)

    def _work(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                job_id, work = self._pending.popleft()
                access_context = self._jobs[job_id].access_context
            try:
                self._mark_running(job_id)
                with bind_workspace_access_context(access_context):
                    result = work(partial(self._update_phase, job_id))
            except Exception as error:  # noqa: BLE001 - background-job boundary
                with self._lock:
                    self._finish_failed(job_id, error)
            else:
                with self._lock:
                    try:
                        self._finish_succeeded(job_id, result)
                    except Exception as error:  # noqa: BLE001 - receipt boundary
                        self._store_failed(job_id, error)

    def _mark_running(self, job_id: str) -> None:
        now = _now()
        job = self._jobs[job_id]
        updated = self._store(
            replace(
                job,
                status=PreflightJobStatus.RUNNING,
                phase=PreflightPhase.VERIFYING,
                message=PREFLIGHT_PHASE_LABELS[PreflightPhase.VERIFYING],
                progress_percent=_PHASE_PERCENT[PreflightPhase.VERIFYING],
                started_at=now,
                updated_at=now,
            )
        )
        self._persist_phase(updated, PreflightPhase.VERIFYING)

    def _update_phase(self, job_id: str, phase: PreflightPhase) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.terminal:
                return
            if _PHASE_PERCENT[phase] < job.progress_percent:
                return
            updated = self._store(
                replace(
                    job,
                    phase=phase,
                    message=PREFLIGHT_PHASE_LABELS[phase],
                    progress_percent=_PHASE_PERCENT[phase],
                    updated_at=_now(),
                )
            )
            self._persist_phase(updated, phase)

    def _finish_succeeded(
        self,
        job_id: str,
        result: PreflightJobResult,
    ) -> PreflightJob:
        now = _now()
        job = self._jobs[job_id]
        if self._attempt_store is not None:
            if result.receipt is None:
                raise PreflightJobStateError(
                    "A durable comparison requires a verified result receipt"
                )
            with timed_preflight_publication("local_attempt_finalization"):
                self._attempt_store.commit_preflight_comparison(
                    job.job_id,
                    job.workspace_id,
                    result={
                        **result.receipt.to_mapping(),
                        "completion_message": result.completion_message,
                        "redirect_url": result.redirect_url,
                    },
                )
        return self._store(
            replace(
                job,
                status=PreflightJobStatus.SUCCEEDED,
                phase=PreflightPhase.COMPLETE,
                message=PREFLIGHT_PHASE_LABELS[PreflightPhase.COMPLETE],
                progress_percent=100,
                preflight_run_id=result.preflight_run_id,
                redirect_url=result.redirect_url,
                completion_message=result.completion_message,
                updated_at=now,
                finished_at=now,
            )
        )

    def _finish_failed(self, job_id: str, error: Exception) -> PreflightJob:
        job = self._jobs[job_id]
        failure = classify_odoo_read_failure(error)
        if self._attempt_store is not None:
            try:
                self._attempt_store.fail_preflight_comparison(
                    job.job_id,
                    job.workspace_id,
                    failure_code=failure.code.value,
                )
            except Exception as persistence_error:  # noqa: BLE001 - boundary
                return self._store_failed(job_id, persistence_error)
        return self._store_failed(job_id, error)

    def _store_failed(self, job_id: str, error: Exception) -> PreflightJob:
        now = _now()
        job = self._jobs[job_id]
        failure = classify_odoo_read_failure(error)
        return self._store(
            replace(
                job,
                status=PreflightJobStatus.FAILED,
                phase=PreflightPhase.COMPLETE,
                message="Comparison needs attention",
                failure=failure,
                updated_at=now,
                finished_at=now,
            )
        )

    def _active_locked(self, workspace_id: str) -> PreflightJob | None:
        return max(
            (
                job
                for job in self._jobs.values()
                if job.workspace_id == workspace_id and job.active
            ),
            key=lambda job: job.created_at,
            default=None,
        )

    def _store(self, job: PreflightJob) -> PreflightJob:
        self._jobs[job.job_id] = job
        return job

    def _new_job(
        self,
        job_id: str,
        workspace_id: str,
        migration_project_name: str,
        access_context: WorkspaceAccessContext,
    ) -> PreflightJob:
        attempt = 1 + max(
            (
                job.attempt
                for job in self._jobs.values()
                if job.workspace_id == workspace_id
            ),
            default=0,
        )
        now = _now()
        return PreflightJob(
            job_id=job_id,
            access_context=access_context,
            workspace_id=workspace_id,
            migration_project_name=(
                migration_project_name.strip()[:300] or "Data project"
            ),
            status=PreflightJobStatus.QUEUED,
            phase=PreflightPhase.QUEUED,
            message=PREFLIGHT_PHASE_LABELS[PreflightPhase.QUEUED],
            progress_percent=0,
            attempt=attempt,
            created_at=now,
            started_at=None,
            updated_at=now,
            finished_at=None,
            preflight_run_id="",
            redirect_url="",
            completion_message="",
            failure=None,
        )

    def _job_from_intent(
        self,
        intent: MigrationOperationIntent,
        migration_project_name: str,
    ) -> PreflightJob:
        detail = intent.detail
        access_context = WorkspaceAccessContext(
            project_id=intent.project_id,
            workspace_id=intent.owner_id,
            data_version_id=str(detail["data_version_id"]),
            migration_run_id=str(detail["migration_run_id"]),
            recipe_application_id=(
                str(detail["recipe_application_id"])
                if detail.get("recipe_application_id")
                else None
            ),
        )
        phase = (
            PreflightPhase(intent.stage)
            if intent.stage in {item.value for item in PreflightPhase}
            else PreflightPhase.QUEUED
        )
        if intent.state is MigrationOperationState.COMMITTED:
            status = PreflightJobStatus.SUCCEEDED
            phase = PreflightPhase.COMPLETE
        elif intent.state is MigrationOperationState.FAILED:
            status = PreflightJobStatus.FAILED
            phase = PreflightPhase.COMPLETE
        else:
            status = (
                PreflightJobStatus.QUEUED
                if phase is PreflightPhase.QUEUED
                else PreflightJobStatus.RUNNING
            )
        result = intent.result
        return PreflightJob(
            job_id=intent.operation_id,
            access_context=access_context,
            workspace_id=intent.owner_id,
            migration_project_name=(
                migration_project_name.strip()[:300] or "Data project"
            ),
            status=status,
            phase=phase,
            message=(
                "Comparison needs attention"
                if status is PreflightJobStatus.FAILED
                else PREFLIGHT_PHASE_LABELS[phase]
            ),
            progress_percent=_PHASE_PERCENT[phase],
            attempt=1,
            created_at=intent.created_at,
            started_at=(
                None
                if intent.stage in {"INTENT_RESERVED", "QUEUED"}
                else intent.updated_at
            ),
            updated_at=intent.updated_at,
            finished_at=(
                intent.updated_at
                if status in {
                    PreflightJobStatus.SUCCEEDED,
                    PreflightJobStatus.FAILED,
                }
                else None
            ),
            preflight_run_id=str(result.get("preflight_run_id", "")),
            redirect_url=str(result.get("redirect_url", "")),
            completion_message=str(result.get("completion_message", "")),
            failure=(
                odoo_read_failure_from_code(intent.last_error)
                if intent.last_error
                else None
            ),
        )

    def _persist_phase(
        self,
        job: PreflightJob,
        phase: PreflightPhase,
    ) -> None:
        if self._attempt_store is not None:
            self._attempt_store.advance_preflight_comparison(
                job.job_id,
                job.workspace_id,
                phase.value,
            )


def _now() -> datetime:
    return datetime.now(UTC)
