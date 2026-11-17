"""Keep durable row transitions and their progress deltas on one boundary."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, Protocol

from impodo.domain.execution.load_control import LoadControl
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)
from impodo.domain.execution.odoo_write import (
    OdooWriteError,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.execution.operator_errors import OperatorErrorRepository
from impodo.domain.execution.write_diagnostics import (
    OdooWriteDiagnostic,
    WriteFailureCode,
)
from impodo.domain.execution_snapshot import ExecutionRow
from impodo.domain.shared.access import Actor
from impodo.domain.workspace.errors import WorkspaceError

from .load_observation import observe_journal_batch, observe_load_local_phase
from .load_transport import LoadControlRepository
from .progress import ExecutionProgressPublisher


class ExecutionJournalRepository(Protocol):
    def close_interrupted_run_for_recomparison(
        self,
        workspace_id: str,
        expected_run: ExecutionRun,
        expected_preflight_run_id: str,
        *,
        actor: Actor,
    ) -> ExecutionRun:
        """Close an interrupted run after ``ExecutionService`` verifies recheck evidence."""
        ...

    def start_run(
        self,
        workspace_id: str,
        run: ExecutionRun,
        *,
        actor: Actor,
        transfer_preflight_hash: str = "",
        intent_assessment_hash: str = "",
        load_control: LoadControl | None = None,
    ) -> None:
        """Durably start the run that ``ExecutionService`` has authorized."""
        ...

    def record_outcomes(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None:
        """Persist terminal row outcomes reported by the Odoo write executor."""
        ...

    def record_batch_started(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
    ) -> None:
        """Persist the uncertain batch boundary before a remote Odoo write."""
        ...

    def record_recovery(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
        *,
        actor: Actor,
    ) -> None:
        """Persist read-back recovery outcomes before an interrupted run resumes."""
        ...

    def finish_run(
        self,
        workspace_id: str,
        run_id: str,
        status: ExecutionRunStatus,
        *,
        actor: Actor,
    ) -> ExecutionRun:
        """Close the journal after the execution service reaches a final outcome."""
        ...

    def get_current_run(
        self,
        workspace_id: str,
        snapshot_hash: str | None = None,
    ) -> ExecutionRun | None:
        """Return the current run, optionally only when its snapshot still matches."""
        ...

    def get_run(self, workspace_id: str, run_id: str) -> ExecutionRun | None:
        """Return one run for execution, recovery, and reconciliation services."""
        ...


class JournalTransitions:
    """Record before/after receipts before updating operation-owned progress."""

    def __init__(
        self,
        journal: ExecutionJournalRepository,
        recorded: dict[str, ExecutionRowAttempt],
        publisher: ExecutionProgressPublisher | None = None,
        *,
        protected_values: Mapping[str, Any] | None = None,
    ) -> None:
        self.journal = journal
        self.recorded = recorded
        self.publisher = publisher
        self.protected_values = protected_values
        self._committed_rows = {
            item.row_id
            for item in recorded.values()
            if item.status is ExecutionRowStatus.COMMITTED
        }

    @property
    def has_committed_rows(self) -> bool:
        """Read the operation's durable receipt index without scanning the journal."""
        return bool(self._committed_rows)

    def _progress_transitions(self, rows: Sequence[ExecutionRowAttempt]) -> None:
        for item in rows:
            if item.status is ExecutionRowStatus.COMMITTED:
                self._committed_rows.add(item.row_id)
            else:
                self._committed_rows.discard(item.row_id)
            if self.publisher is not None:
                self.publisher.transition(self.recorded[item.row_id], item)

    def record_rejected_batch(
        self,
        controls: LoadControlRepository,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
        error: OdooWriteRejected,
    ) -> None:
        """Save a proven rollback before making split attempts eligible again."""
        retries = controls.record_rejected_batch(workspace_id, run_id, rows, error)
        self._progress_transitions(retries)
        self.recorded.update({item.row_id: item for item in retries})

    def record_outcomes(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
        *,
        error: OdooWriteError | None = None,
        protected_values: Mapping[str, Any] | None = None,
    ) -> None:
        """Measure each durable outcome write without changing its boundary."""

        protected_values = (
            protected_values if protected_values is not None else self.protected_values
        )
        with observe_load_local_phase("journal_after_call"):
            detail = error.operator_detail if error is not None else None
            if detail is not None and isinstance(self.journal, OperatorErrorRepository):
                detail = detail.redact(
                    tuple(str(value) for value in (protected_values or {}).values())
                )
                self.journal.record_outcomes_with_detail(
                    workspace_id, run_id, rows, detail
                )
            else:
                self.journal.record_outcomes(workspace_id, run_id, rows)

        self._progress_transitions(rows)

    def start_transport_batch(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRow],
        recorded: dict[str, ExecutionRowAttempt],
        *,
        phase: str,
        component: int,
        page: int,
        batch: int,
        known_ids: Mapping[str, int | None] | None = None,
    ) -> None:
        """Persist one exact in-flight transport batch before sending it."""

        if phase not in {"CREATE", "UPDATE", "COMPLETION"}:
            raise WorkspaceError(
                "Execution transport phase is invalid",
                load_failure_code="LOAD_EXECUTION_TRANSPORT_PHASE_IS_INVALID",
            )
        attempts = tuple(
            replace(
                recorded[row.row_id],
                status=ExecutionRowStatus.IN_FLIGHT,
                attempt=recorded[row.row_id].attempt + 1,
                odoo_id=(known_ids or {}).get(row.row_id),
                safe_error="",
                diagnostic=None,
                schedule_component=component,
                transport_page=page,
                transport_batch=batch,
                transport_phase=phase,
            )
            for row in rows
        )
        with observe_load_local_phase("journal_before_call"):
            self.journal.record_batch_started(
                workspace_id,
                run_id,
                attempts,
            )
        observe_journal_batch(attempts)
        self._progress_transitions(attempts)
        recorded.update({item.row_id: item for item in attempts})

    def record_blocked(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRow],
        recorded: dict[str, ExecutionRowAttempt],
        reason: str,
    ) -> None:
        outcomes = tuple(
            replace(
                recorded[row.row_id],
                status=ExecutionRowStatus.BLOCKED,
                safe_error=reason,
            )
            for row in rows
            if recorded[row.row_id].status
            in {
                ExecutionRowStatus.PLANNED,
                ExecutionRowStatus.RETRY_READY,
            }
        )
        self.record_outcomes(workspace_id, run_id, outcomes)
        recorded.update({item.row_id: item for item in outcomes})


def require_dependency_receipts(
    row: ExecutionRow,
    row_by_id: Mapping[str, ExecutionRow],
    recorded: Mapping[str, ExecutionRowAttempt],
) -> None:
    """Require journalled create receipts before a dependent write."""

    for intent in row.fields:
        if intent.defer_on_create:
            continue
        for dependency_row_id in intent.dependency_row_ids:
            dependency = row_by_id[dependency_row_id]
            if dependency.disposition != "CREATE":
                continue
            attempt = recorded.get(dependency_row_id)
            if (
                attempt is None
                or attempt.status
                not in {
                    ExecutionRowStatus.COMMITTED,
                    ExecutionRowStatus.PARTIALLY_APPLIED,
                }
                or attempt.odoo_id is None
            ):
                raise WorkspaceError(
                    "A required Odoo create receipt was not journalled",
                    load_failure_code="LOAD_A_REQUIRED_ODOO_CREATE_RECEIPT_WAS_NOT_JOURNALLED",
                )
            if intent.incoming_projection_field and not any(
                receipt.projection_field == intent.incoming_projection_field
                and receipt.target_model == intent.related_model
                for receipt in attempt.projected_receipts
            ):
                raise WorkspaceError(
                    "A generated Odoo relationship receipt was not journalled",
                    load_failure_code="LOAD_A_GENERATED_ODOO_RELATIONSHIP_RECEIPT_WAS_NOT_JOURNALLED",
                )


def write_diagnostic(
    error: OdooWriteError,
    row: ExecutionRow,
    batch_size: int = 1,
    *,
    method: str | None = None,
) -> OdooWriteDiagnostic:
    """Always retain operation context, even when Odoo supplies no useful detail."""

    return error.diagnostic or OdooWriteDiagnostic(
        code=(
            WriteFailureCode.UNKNOWN
            if isinstance(error, OdooWriteOutcomeUnknown)
            else WriteFailureCode.REJECTED
        ),
        model=row.target_model,
        method=method or ("create" if row.disposition == "CREATE" else "write"),
        batch_size=batch_size,
    )
