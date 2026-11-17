"""Publish load progress from journal transitions without retaining journal rows."""

from __future__ import annotations

from bisect import bisect_left, insort
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from heapq import heappop, heappush

from impodo.domain.execution.load_control import LoadControlState
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
    ExecutionRunStatus,
)

from .job_models import LoadPhase
from .load_observation import observe_load_local_phase

_UNFINISHED = frozenset(
    {
        ExecutionRowStatus.PLANNED,
        ExecutionRowStatus.IN_FLIGHT,
        ExecutionRowStatus.RETRY_READY,
    }
)
_INCOMPLETE = _UNFINISHED | {ExecutionRowStatus.PARTIALLY_APPLIED}
_ATTENTION = frozenset(
    {
        ExecutionRowStatus.FAILED,
        ExecutionRowStatus.BLOCKED,
        ExecutionRowStatus.OUTCOME_UNKNOWN,
    }
)
_CREATED = frozenset(
    {
        ExecutionRowStatus.COMMITTED,
        ExecutionRowStatus.PARTIALLY_APPLIED,
    }
)


@dataclass(frozen=True, slots=True)
class ExecutionProgress:
    """Immutable non-sensitive counters; this value never authorizes a write."""

    run_id: str
    workspace_id: str
    status: ExecutionRunStatus
    phase: LoadPhase
    total_count: int
    completed_rows: int
    created_count: int
    updated_count: int
    attention_count: int
    planned_count: int
    in_flight_count: int
    retry_ready_count: int
    committed_count: int
    partially_applied_count: int
    failed_count: int
    blocked_count: int
    unknown_count: int
    rejected_count: int
    unattempted_count: int
    unfinished_first_pass: int
    relationship_total_count: int
    load_group_number: int
    load_group_count: int
    active_component: int | None
    load_control_state: str
    load_pause_reason: str


class ExecutionProgressAccumulator:
    """Own one load's counters, initialized once from its durable journal.

    Call ``transition`` only after a successful journal mutation. Keeping the
    previous and new contribution separate prevents a completed relationship
    from counting the same create twice. No row collection is retained here.
    """

    def __init__(
        self,
        run: ExecutionRun,
        *,
        phase: LoadPhase = LoadPhase.WRITING,
        relationship_total_count: int = 0,
        load_group_count: int = 0,
    ) -> None:
        self._run_id = run.run_id
        self._workspace_id = run.workspace_id
        self._status = run.status
        self._phase = phase
        self._control = run.load_control
        self._states: Counter[ExecutionRowStatus] = Counter()
        self._total = 0
        self._completed = 0
        self._created = 0
        self._updated = 0
        self._rejected = 0
        self._relationship_total = 0
        self._group_count_floor = load_group_count
        self._components: list[int] = []
        self._known_components: set[int] = set()
        self._component_members: Counter[int] = Counter()
        self._component_pending: Counter[int] = Counter()
        self._active_components: list[int] = []
        self._heap_components: set[int] = set()
        for row in run.rows:
            self._total += 1
            self._contribute(row, 1)
        self._components = sorted(self._component_members)
        self._known_components = set(self._components)
        self._relationship_total = max(
            relationship_total_count, self._states[ExecutionRowStatus.PARTIALLY_APPLIED]
        )

    def transition(
        self, previous: ExecutionRowAttempt, current: ExecutionRowAttempt
    ) -> None:
        """Apply only the two row contributions involved in a saved transition."""
        self._contribute(previous, -1)
        self._contribute(current, 1)
        if (
            current.schedule_component >= 0
            and current.schedule_component not in self._known_components
        ):
            insort(self._components, current.schedule_component)
            self._known_components.add(current.schedule_component)
        self._relationship_total = max(
            self._relationship_total,
            self._states[ExecutionRowStatus.PARTIALLY_APPLIED],
        )

    def finish(self, run: ExecutionRun) -> None:
        """Use terminal/control metadata without inspecting the returned rows."""
        self._status = run.status
        self._control = run.load_control

    def snapshot(self) -> ExecutionProgress:
        """Build a constant-size publication from counters and the active group."""
        unfinished = sum(self._states[state] for state in _UNFINISHED)
        partial = self._states[ExecutionRowStatus.PARTIALLY_APPLIED]
        if self._phase is LoadPhase.WRITING and not unfinished and partial:
            self._phase = LoadPhase.RELATIONSHIPS
        while (
            self._active_components
            and not self._component_pending[self._active_components[0]]
        ):
            self._heap_components.remove(heappop(self._active_components))
        group_count = max(len(self._components), self._group_count_floor)
        group_number = (
            bisect_left(self._components, self._active_components[0]) + 1
            if self._active_components
            else group_count
        )
        return ExecutionProgress(
            run_id=self._run_id,
            workspace_id=self._workspace_id,
            status=self._status,
            phase=self._phase,
            total_count=self._total,
            completed_rows=self._completed,
            created_count=self._created,
            updated_count=self._updated,
            attention_count=sum(self._states[state] for state in _ATTENTION)
            + (partial if self._status is not ExecutionRunStatus.RUNNING else 0),
            planned_count=self._states[ExecutionRowStatus.PLANNED],
            in_flight_count=self._states[ExecutionRowStatus.IN_FLIGHT],
            retry_ready_count=self._states[ExecutionRowStatus.RETRY_READY],
            committed_count=self._states[ExecutionRowStatus.COMMITTED],
            partially_applied_count=partial,
            failed_count=self._states[ExecutionRowStatus.FAILED],
            blocked_count=self._states[ExecutionRowStatus.BLOCKED],
            unknown_count=self._states[ExecutionRowStatus.OUTCOME_UNKNOWN],
            rejected_count=self._rejected,
            unattempted_count=self._states[ExecutionRowStatus.PLANNED]
            + self._states[ExecutionRowStatus.RETRY_READY],
            unfinished_first_pass=unfinished,
            relationship_total_count=self._relationship_total,
            load_group_number=group_number,
            load_group_count=group_count,
            active_component=self._active_components[0]
            if self._active_components
            else None,
            load_control_state=self._control.state.value if self._control else "",
            load_pause_reason=self._control.reason if self._control else "",
        )

    def _contribute(self, row: ExecutionRowAttempt, direction: int) -> None:
        status = row.status
        self._states[status] += direction
        self._completed += direction * (status not in _INCOMPLETE)
        self._created += direction * (row.operation == "CREATE" and status in _CREATED)
        self._updated += direction * (
            row.operation == "UPDATE" and status is ExecutionRowStatus.COMMITTED
        )
        self._rejected += direction * (
            status is ExecutionRowStatus.FAILED
            or (
                status is ExecutionRowStatus.PARTIALLY_APPLIED
                and row.diagnostic is not None
            )
        )
        component = row.schedule_component
        if component < 0:
            return
        self._component_members[component] += direction
        if status in _UNFINISHED:
            if direction > 0 and component not in self._heap_components:
                heappush(self._active_components, component)
                self._heap_components.add(component)
            self._component_pending[component] += direction


class ExecutionProgressPublisher:
    """Throttle intermediate counters while publishing boundaries immediately."""

    def __init__(
        self,
        run: ExecutionRun,
        sink: Callable[[ExecutionProgress], None] | None,
        *,
        row_interval: int,
    ) -> None:
        self._accumulator = ExecutionProgressAccumulator(run)
        self._sink = sink
        self._row_interval = max(1, row_interval)
        self._changed = 0
        self._external_stop_reconciled = False

    def transition(
        self, previous: ExecutionRowAttempt, current: ExecutionRowAttempt
    ) -> None:
        """Advance progress after the caller has durably recorded the outcome."""
        self._accumulator.transition(previous, current)

    def publish(self, *, changed_rows: int = 0, force: bool = False) -> None:
        """Publish counters without reading or copying the complete journal."""
        self._changed += max(0, changed_rows)
        if self._sink is None or (not force and self._changed < self._row_interval):
            return
        with observe_load_local_phase("progress_publication"):
            self._sink(self.snapshot())
        self._changed = 0

    def finish(self, run: ExecutionRun) -> None:
        """Publish the authoritative terminal state and latest control metadata."""
        self._accumulator.finish(run)
        self.publish(force=True)

    def snapshot(self) -> ExecutionProgress:
        """Return current compact counters without publishing them."""
        return self._accumulator.snapshot()

    def reconcile_stopped_run(self, run: ExecutionRun) -> None:
        """Rebase once after another operation stops and settles journal rows.

        The external stop command owns those mutations, so their deltas cannot
        reach this operation's accumulator. This exceptional O(N) recovery read
        is separate from ordinary intermediate publications and metadata-only
        ``finish``. Repeated notifications do not rescan the same stopped run.
        """
        history = self.snapshot()
        if (
            run.run_id != history.run_id
            or run.workspace_id != history.workspace_id
            or run.status is ExecutionRunStatus.RUNNING
            or run.load_control is None
            or run.load_control.state is not LoadControlState.STOPPED
        ):
            raise ValueError("Progress reconciliation requires the same stopped run")
        if self._external_stop_reconciled:
            self.finish(run)
            return
        self._accumulator = ExecutionProgressAccumulator(
            run,
            phase=history.phase,
            relationship_total_count=history.relationship_total_count,
            load_group_count=history.load_group_count,
        )
        self._external_stop_reconciled = True
        self.publish(force=True)


def execution_progress(
    run: ExecutionRun, *, phase: LoadPhase = LoadPhase.WRITING
) -> ExecutionProgress:
    """Initialize compact counters at an admission or recovery boundary."""
    return ExecutionProgressAccumulator(run, phase=phase).snapshot()
