"""Isolate confirmed rejected create batches through journal-owned callbacks.

Only known rolled-back data rejections are split. Every child attempt has a
new transport identity, the original row values and the original External IDs.
An uncertain result immediately ends this generator without resending it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from impodo.domain.execution.load_control import LoadControl, can_set_aside
from impodo.domain.execution.models import ExecutionRowAttempt
from impodo.domain.execution.odoo_write import (
    OdooWriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)
from impodo.domain.execution_snapshot import ExecutionRow, FieldIntent
from impodo.domain.shared.access import Actor

from .load_observation import observe_load_transport


class LoadControlRepository(Protocol):
    """Supply short local control operations, never an open database owner."""

    def get_load_control(
        self, workspace_id: str, run_id: str
    ) -> LoadControl | None: ...
    def request_load_stop(
        self, workspace_id: str, run_id: str, *, actor: Actor
    ) -> LoadControl: ...
    def resume_load_control(
        self, workspace_id: str, run_id: str, *, expected_hash: str, actor: Actor
    ) -> LoadControl: ...
    def record_rejected_batch(
        self,
        workspace_id: str,
        run_id: str,
        rows: Sequence[ExecutionRowAttempt],
        error: OdooWriteRejected,
    ) -> tuple[ExecutionRowAttempt, ...]: ...

PreparedCreate = tuple[ExecutionRow, dict[str, Any], tuple[FieldIntent, ...]]


class DependentCompletions:
    """Set aside unfinished consumers using only frozen incoming row edges."""

    def __init__(self, rows: Sequence[ExecutionRow], failed: Sequence[str]) -> None:
        self._consumers: dict[str, set[str]] = {}
        self._blocked: set[str] = set()
        for row in rows:
            for intent in row.fields:
                for parent in intent.dependency_row_ids:
                    self._consumers.setdefault(parent, set()).add(row.row_id)
        for row_id in failed:
            self.reject(row_id)

    def reject(self, row_id: str) -> None:
        """Propagate one known failure once, including cyclic consumers."""
        pending = [row_id]
        while pending:
            current = pending.pop()
            if current in self._blocked:
                continue
            self._blocked.add(current)
            pending.extend(self._consumers.get(current, ()))

    def blocks(self, row_id: str) -> bool:
        """Keep completed receipts intact while preventing dependent completions."""
        return row_id in self._blocked


@dataclass(frozen=True, slots=True)
class CreateTransportResult:
    """Return receipts or one definitive/uncertain failure for an actual call."""

    prepared: tuple[PreparedCreate, ...]
    identifiers: tuple[int, ...] = ()
    error: OdooWriteRejected | OdooWriteOutcomeUnknown | None = None


def create_transport_results(
    prepared: Sequence[PreparedCreate],
    *,
    model: str,
    executor: OdooWriteExecutor,
    import_create: bool,
    continuation: bool,
    begin: Callable[[tuple[ExecutionRow, ...]], None],
    rolled_back: Callable[[tuple[ExecutionRow, ...], OdooWriteRejected], None],
) -> Iterator[CreateTransportResult]:
    """Bisect known rollback results deterministically and preserve all receipts."""
    pending = deque([tuple(prepared)])
    while pending:
        group = pending.popleft()
        begin(tuple(item[0] for item in group))
        try:
            values: tuple[Mapping[str, Any], ...] = tuple(item[1] for item in group)
            fields = tuple(sorted({field for value in values for field in value}))
            with observe_load_transport(model, "load" if import_create else "create", fields, len(group)):
                identifiers = (
                    executor.load_create_rows(
                        model, values, tuple(item[0].proposed_external_id for item in group)
                    )
                    if import_create
                    else executor.create_rows(model, values)
                )
        except OdooWriteOutcomeUnknown as error:
            yield CreateTransportResult(group, error=error)
            return
        except OdooWriteRejected as error:
            if continuation and can_set_aside(error) and len(group) > 1:
                rolled_back(tuple(item[0] for item in group), error)
                middle = len(group) // 2
                pending.appendleft(group[middle:])
                pending.appendleft(group[:middle])
                continue
            yield CreateTransportResult(group, error=error)
            if not continuation or not can_set_aside(error):
                return
        else:
            yield CreateTransportResult(group, identifiers)
