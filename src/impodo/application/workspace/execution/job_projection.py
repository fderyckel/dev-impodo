"""Project compact saved load counters into browser job presentation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from .job_models import LOAD_PHASE_LABELS, LoadJob, LoadPhase
from .progress import ExecutionProgress


def project_load_progress(
    job: LoadJob,
    progress: ExecutionProgress,
    *,
    phase: LoadPhase | None = None,
) -> LoadJob:
    """Preserve monotonic presentation without reading any journal rows."""
    effective_phase = phase or progress.phase
    if (
        effective_phase is LoadPhase.WRITING
        and not progress.unfinished_first_pass
        and (progress.partially_applied_count or job.phase is LoadPhase.RELATIONSHIPS)
    ):
        effective_phase = LoadPhase.RELATIONSHIPS
    settled = progress.total_count - progress.unfinished_first_pass
    fraction = settled / progress.total_count if progress.total_count else 0.0
    percent = min(82, 8 + round(74 * max(0.0, min(1.0, fraction))))
    relationship_total = max(
        job.relationship_total_count,
        progress.relationship_total_count,
        progress.partially_applied_count,
    )
    if effective_phase is LoadPhase.RELATIONSHIPS:
        completed = max(0, relationship_total - progress.partially_applied_count)
        fraction = completed / relationship_total if relationship_total else 1.0
        percent = min(89, 82 + round(7 * max(0.0, min(1.0, fraction))))
    group_count = max(job.load_group_count, progress.load_group_count)
    group_number = (
        progress.load_group_number
        if progress.active_component is not None
        else group_count
    )
    control_state = progress.load_control_state
    pause_reason = progress.load_pause_reason
    if (
        job.load_control_state == "STOP_REQUESTED"
        and control_state not in {"STOPPED", "FINISHED"}
    ):
        # The stop was recorded after this worker snapshot was captured.
        control_state = "STOP_REQUESTED"
        pause_reason = job.load_pause_reason
    message = LOAD_PHASE_LABELS[effective_phase]
    if effective_phase is LoadPhase.WRITING and group_number and group_count:
        message = f"Sending load group {group_number} of {group_count} to Odoo"
    if control_state == "PAUSED":
        message = "Upload paused after repeated failures; review the saved results"
    elif control_state == "STOP_REQUESTED":
        message = "Stopping after the current request"
    return replace(
        job,
        phase=effective_phase,
        message=message,
        total_rows=progress.total_count,
        completed_rows=progress.completed_rows,
        created_count=progress.created_count,
        updated_count=progress.updated_count,
        attention_count=progress.attention_count,
        relationship_pending_count=progress.partially_applied_count,
        relationship_total_count=relationship_total,
        load_group_number=group_number,
        load_group_count=group_count,
        progress_percent=max(job.progress_percent, percent),
        execution_run_id=progress.run_id,
        load_control_state=control_state,
        load_pause_reason=pause_reason,
        rejected_count=progress.rejected_count,
        blocked_count=progress.blocked_count,
        unattempted_count=progress.unattempted_count,
        updated_at=datetime.now(UTC),
    )


def project_load_verification(job: LoadJob) -> LoadJob:
    """Advance the phase while retaining saved counts and stop/pause wording."""
    message = {
        "PAUSED": "Upload paused after repeated failures; review the saved results",
        "STOP_REQUESTED": "Stopping after the current request",
    }.get(job.load_control_state, LOAD_PHASE_LABELS[LoadPhase.VERIFYING])
    return replace(
        job,
        phase=LoadPhase.VERIFYING,
        message=message,
        progress_percent=max(90, job.progress_percent),
        updated_at=datetime.now(UTC),
    )
