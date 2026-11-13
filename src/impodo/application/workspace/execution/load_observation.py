"""Publish value-free load evidence without owning execution or storage.

The HTTP boundary binds an observer and the load manager explicitly carries it
to its worker. Execution reports durable journal observations and transport
boundaries through this optional port. Observer failures never change writes,
receipts, exception propagation, or recovery decisions.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
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
from impodo.domain.odoo.contracts import (
    ConnectorAuthenticationError,
    ConnectorAuthorizationError,
    ConnectorConfigurationError,
    ConnectorIncompleteResultError,
    ConnectorTransportError,
)

LoadEvidenceSink = Callable[[str, Mapping[str, object]], None]
LoadStageMetrics = Callable[[bool], AbstractContextManager[dict[str, object]]]
LOAD_LOCAL_PHASES = frozenset({
    "journal_before_call", "journal_after_call", "progress_publication",
    "worker_admission", "execution_checks",
})
_LOCAL_METRICS = frozenset({
    "wall_ms", "thread_cpu_ms", "database_connection_count",
    "database_schema_check_count", "database_lock_retry_count", "db_connect_ms",
    "db_schema_ms", "db_lock_wait_ms", "capture_failure_count",
})
_OBSERVER: ContextVar[LoadObservation | None] = ContextVar(
    "load_observation", default=None
)
_PACKAGE_ROOT = Path(__file__).resolve().parents[3]
_CONNECTOR_REASONS = (
    (ConnectorAuthenticationError, "LOAD_ODOO_AUTHENTICATION_FAILED"),
    (ConnectorAuthorizationError, "LOAD_ODOO_READ_ACCESS_DENIED"),
    (ConnectorConfigurationError, "LOAD_ODOO_READ_CONFIGURATION_INVALID"),
    (ConnectorIncompleteResultError, "LOAD_ODOO_READ_INCOMPLETE"),
    (ConnectorTransportError, "LOAD_ODOO_READ_TRANSPORT_FAILED"),
)


@dataclass(slots=True)
class LoadObservation:
    """Carry one opaque attempt and its optional sink across worker entry."""

    attempt_id: str
    sink: LoadEvidenceSink
    enqueued: bool = False
    terminal: bool = False
    stage: str = "submission"
    last_error_id: int = 0
    stage_metrics: LoadStageMetrics | None = None
    local_phases: dict[str, dict[str, int | float]] = field(default_factory=dict)
    local_phase_calls: int = 0
    local_phase_checkpoints: int = 0
    _metrics_lock: RLock = field(default_factory=RLock, repr=False)

    def emit(self, event: str, **facts: object) -> None:
        """Offer bounded evidence without allowing diagnostics to stop work."""
        try:
            if event in {"stage_finished", "performance_checkpoint", "terminal"}:
                with self._metrics_lock:
                    facts["local_phase_metrics"] = {
                        name: dict(metrics) for name, metrics in self.local_phases.items()
                    }
                    facts["local_phase_checkpoint_count"] = self.local_phase_checkpoints
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


def record_load_failure(
    error: BaseException,
    *,
    stage: str = "",
    model: str = "",
    fields: tuple[str, ...] = (),
) -> None:
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
        if not isinstance(code, str) or code not in LOAD_FAILURE_REASONS:
            code = next(
                (
                    reason
                    for error_type, reason in _CONNECTOR_REASONS
                    if isinstance(candidate, error_type)
                ),
                "LOAD_UNCLASSIFIED",
            )
        causes.append(
            {
                "exception_class": type(candidate).__name__,
                "code": code,
                "frames": frames[-8:],
                "omitted_frame_count": max(0, len(frames) - 8),
            }
        )
        if isinstance(candidate, ConnectorTransportError):
            category = getattr(candidate, "category", None)
            count = getattr(candidate, "attempt_count", None)
            if isinstance(category, str) and category in {"timeout", "unreachable"}:
                causes[-1]["failure_category"] = category
            if type(count) is int and 1 <= count <= 100:
                causes[-1]["attempt_count"] = count
        candidate = candidate.__cause__ or (
            None if candidate.__suppress_context__ else candidate.__context__
        )
    observer.emit(
        "failure",
        stage=stage or observer.stage,
        causes=causes,
        cause_chain_truncated=candidate is not None,
        model=model,
        fields=list(fields),
    )


@contextmanager
def _stage_metric_scope(
    observer: LoadObservation, *, sample_memory: bool,
) -> Iterator[dict[str, object]]:
    """Isolate optional provider entry and exit without swallowing body errors."""
    manager = None
    metrics: dict[str, object] = {}
    if observer.stage_metrics is not None:
        try:
            manager = observer.stage_metrics(sample_memory)
            observed = manager.__enter__()
            metrics = observed if isinstance(observed, dict) else {"capture_failure_count": 1}
        except Exception:  # noqa: BLE001 - optional observer setup must not skip work
            manager = None
            metrics = {"capture_failure_count": 1}
    try:
        yield metrics
    finally:
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - telemetry exit cannot replace body errors
                metrics["capture_failure_count"] = 1


@contextmanager
def observe_load_local_phase(stage: str) -> Iterator[None]:
    """Aggregate fixed local boundaries without one logging event per row."""
    observer = _OBSERVER.get()
    if observer is None or stage not in LOAD_LOCAL_PHASES:
        yield
        return
    started = perf_counter()
    metrics: dict[str, object] = {}
    try:
        with _stage_metric_scope(observer, sample_memory=False) as metrics:
            yield
    finally:
        checkpoint = False
        try:
            metrics["wall_ms"] = max(0.0, (perf_counter() - started) * 1000)
            with observer._metrics_lock:
                aggregate = observer.local_phases.setdefault(stage, {"calls": 0})
                aggregate["calls"] += 1
                for name in _LOCAL_METRICS:
                    value = metrics.get(name)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        if 0 <= value <= 2**53 - 1 and math.isfinite(value):
                            aggregate[name] = min(2**53 - 1, aggregate.get(name, 0) + value)
                        else:
                            aggregate["capture_failure_count"] = aggregate.get("capture_failure_count", 0) + 1
                observer.local_phase_calls += 1
                if observer.local_phase_calls % 128 == 0 and observer.local_phase_checkpoints < 16:
                    observer.local_phase_checkpoints += 1
                    checkpoint = True
            if checkpoint:
                observer.emit("performance_checkpoint")
        except Exception:  # noqa: BLE001 - invalid optional metrics cannot replace body exceptions
            with observer._metrics_lock:
                aggregate = observer.local_phases.setdefault(stage, {"calls": 0})
                aggregate["capture_failure_count"] = aggregate.get("capture_failure_count", 0) + 1


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
    outcome = "COMPLETED"
    metrics: dict[str, object] = {}
    try:
        with _stage_metric_scope(observer, sample_memory=True) as metrics:
            try:
                yield
            except BaseException as error:
                outcome = "FAILED"
                record_load_failure(error, stage=stage)
                raise
    finally:
        observer.emit(
            "stage_finished", stage=stage, outcome=outcome,
            duration_ms=(perf_counter() - started) * 1000,
            performance=metrics,
        )
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
        snapshot_contract_version=snapshot.contract_version,
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
        rejection_policy=run.load_control.policy.value if run.load_control else "STOP",
        load_policy_version=run.load_control.contract_version
        if run.load_control
        else 0,
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
