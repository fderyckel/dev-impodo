"""Publish value-free load evidence without owning execution or storage.

The HTTP boundary binds an observer and the load manager explicitly carries it
to its worker. Execution reports durable journal observations and transport
boundaries through this optional port. Observer failures never change writes,
receipts, exception propagation, or recovery decisions.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from impodo.domain.execution.load_failure_reasons import LOAD_FAILURE_REASONS
from impodo.domain.execution.models import (
    ExecutionRowAttempt,
    ExecutionRowStatus,
    ExecutionRun,
)
from impodo.domain.execution.odoo_write import OdooWriteError, OdooWriteRejected
from impodo.domain.execution_snapshot import ExecutionSnapshot
from impodo.domain.odoo.compatibility import OdooOperation, assess_odoo_operation

LoadEvidenceSink = Callable[[str, Mapping[str, object]], None]
_OBSERVER: ContextVar[LoadObservation | None] = ContextVar(
    "load_observation", default=None
)
_PACKAGE_ROOT = Path(__file__).resolve().parents[3]


@dataclass(slots=True)
class LoadObservation:
    """Carry one opaque attempt and its optional sink across worker entry."""

    attempt_id: str
    sink: LoadEvidenceSink
    enqueued: bool = False
    terminal: bool = False
    stage: str = "submission"
    last_error_id: int = 0

    def emit(self, event: str, **facts: object) -> None:
        """Offer bounded evidence without allowing diagnostics to stop work."""
        try:
            self.sink(event, {"attempt_id": self.attempt_id, **facts})
        except Exception:  # noqa: BLE001 - optional diagnostic sink boundary
            return


def current_load_observation() -> LoadObservation | None:
    """Return only the observation explicitly bound to this operation."""
    return _OBSERVER.get()


@contextmanager
def bind_load_observation(observer: LoadObservation | None) -> Iterator[None]:
    """Bind and restore one observation without propagating other context."""
    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def emit_load_evidence(event: str, **facts: object) -> None:
    """Publish an execution fact when a load diagnostic observer is present."""
    if (observer := _OBSERVER.get()) is not None:
        observer.emit(event, **facts)


def record_load_failure(error: BaseException, *, stage: str = "") -> None:
    """Retain coded reasons and approved frames, never exception messages."""
    observer = _OBSERVER.get()
    if observer is None or observer.last_error_id == id(error):
        return
    observer.last_error_id = id(error)
    causes: list[dict[str, object]] = []
    seen: set[int] = set()
    candidate: BaseException | None = error
    while candidate is not None and len(causes) < 5 and id(candidate) not in seen:
        seen.add(id(candidate))
        frames: list[dict[str, object]] = []
        trace = candidate.__traceback__
        while trace is not None:
            path = Path(trace.tb_frame.f_code.co_filename)
            if path.is_relative_to(_PACKAGE_ROOT):
                frames.append(
                    {
                        "module": "impodo/"
                        + path.relative_to(_PACKAGE_ROOT).as_posix(),
                        "function": trace.tb_frame.f_code.co_name,
                        "line": trace.tb_lineno,
                    }
                )
            trace = trace.tb_next
        code = getattr(candidate, "load_failure_code", "")
        if code not in LOAD_FAILURE_REASONS:
            code = "LOAD_UNCLASSIFIED"
        causes.append(
            {
                "exception_class": type(candidate).__name__,
                "code": code,
                "frames": frames[-8:],
                "omitted_frame_count": max(0, len(frames) - 8),
            }
        )
        candidate = candidate.__cause__ or (
            None if candidate.__suppress_context__ else candidate.__context__
        )
    observer.emit(
        "failure",
        stage=stage or observer.stage,
        causes=causes,
        cause_chain_truncated=candidate is not None,
    )


@contextmanager
def observe_load_stage(stage: str) -> Iterator[None]:
    """Record both ends of a stage while preserving its original exception."""
    observer = _OBSERVER.get()
    if observer is None:
        yield
        return
    previous = observer.stage
    observer.stage = stage
    started = perf_counter()
    observer.emit("stage_started", stage=stage)
    try:
        yield
    except BaseException as error:
        record_load_failure(error, stage=stage)
        observer.emit(
            "stage_finished",
            stage=stage,
            outcome="FAILED",
            duration_ms=(perf_counter() - started) * 1000,
        )
        raise
    else:
        observer.emit(
            "stage_finished",
            stage=stage,
            outcome="COMPLETED",
            duration_ms=(perf_counter() - started) * 1000,
        )
    finally:
        observer.stage = previous


def observe_load_context(
    snapshot: ExecutionSnapshot, *, source_mode: str, connection_mode: str
) -> None:
    """Record the verified target major without retaining target identities."""
    if _OBSERVER.get() is None:
        return
    version = assess_odoo_operation(
        snapshot.target_odoo_version, OdooOperation.WRITE
    ).version
    emit_load_evidence(
        "context",
        source_mode=source_mode,
        connection_mode=connection_mode,
        odoo_major=version.major,
    )


def observe_load_journal(run: ExecutionRun, *, projection: str = "OBSERVED") -> None:
    """Project one already-read durable journal without source identifiers."""
    if _OBSERVER.get() is None:
        return
    counts = Counter(row.status.value for row in run.rows)
    groups: Counter[tuple[str, str, str, tuple[str, ...], int | None, str | None]] = (
        Counter()
    )
    for row in run.rows:
        if (diagnostic := row.diagnostic) is not None:
            groups[
                (
                    diagnostic.code.value,
                    diagnostic.model,
                    diagnostic.method,
                    diagnostic.fields[:20],
                    diagnostic.http_status,
                    diagnostic.error_type,
                )
            ] += 1
    selected = groups.most_common(40)
    emit_load_evidence(
        "journal",
        projection=projection,
        execution_run_id=run.run_id,
        journal_status=run.status.value,
        row_states={state.value: counts[state.value] for state in ExecutionRowStatus},
        batch_rows=run.batch_rows,
        load_control_state=run.load_control.state.value if run.load_control else "",
        pause_reason=run.load_control.reason if run.load_control else "",
        diagnostics=[
            {
                "code": code,
                "model": model,
                "method": method,
                "fields": list(fields),
                "http_status": status,
                "error_type": error_type,
                "affected_rows": count,
            }
            for (code, model, method, fields, status, error_type), count in selected
        ],
        omitted_group_count=max(0, len(groups) - len(selected)),
    )


@contextmanager
def observe_load_transport(
    model: str, method: str, fields: Sequence[str], batch_size: int
) -> Iterator[None]:
    """Observe a write call without reading its values, keys, or receipts."""
    if _OBSERVER.get() is None:
        yield
        return
    transport_id = str(uuid4())
    emit_load_evidence(
        "transport_started",
        transport_id=transport_id,
        model=model,
        method=method,
        fields=list(fields)[:20],
        batch_size=batch_size,
        omitted_field_count=max(0, len(fields) - 20),
    )
    try:
        yield
    except BaseException as error:
        diagnostic = error.diagnostic if isinstance(error, OdooWriteError) else None
        emit_load_evidence(
            "transport_finished",
            transport_id=transport_id,
            outcome="REJECTED" if isinstance(error, OdooWriteRejected) else "UNKNOWN",
            diagnostics=[diagnostic.to_dict()] if diagnostic is not None else [],
        )
        raise
    else:
        emit_load_evidence(
            "transport_finished", transport_id=transport_id, outcome="RETURNED"
        )


def observe_journal_batch(rows: Sequence[ExecutionRowAttempt]) -> None:
    """Describe an already-persisted in-flight batch without its row IDs."""
    if rows and _OBSERVER.get() is not None:
        row = rows[0]
        emit_load_evidence(
            "journal_batch",
            transport_phase=row.transport_phase,
            schedule_component=row.schedule_component,
            transport_page=row.transport_page,
            transport_batch=row.transport_batch,
            batch_size=len(rows),
        )
