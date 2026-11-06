"""Privacy-safe local diagnostics for the loopback browser application.

The recorder accepts only bounded operational fields. Request URLs, query
strings, headers, bodies, source values, formulas, credentials, and tokens are
deliberately outside its API so routine diagnostics cannot capture them by
accident.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import platform
import re
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock
from time import perf_counter, sleep
from typing import Any
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from starlette.datastructures import MutableHeaders

from impodo.adapters.duckdb.request_timing import (
    collect_duckdb_request_timings,
)
from impodo.adapters.odoo.connectors import (
    Json2ReadFailureDiagnostic,
    observe_json2_read_failures,
)
from impodo.application.shared.build_contract import ApplicationBuildContract
from impodo.application.workspace.execution.load_observation import (
    bind_load_observation,
    record_load_failure,
)
from impodo.domain.execution.load_control import FailureWindow, LoadControl
from impodo.domain.execution.write_diagnostics import OdooWriteErrorType
from impodo.domain.mapping.mutations import MappingMutationAction
from impodo.web.load_diagnostic_bundle import load_bundle_members
from impodo.web.load_diagnostics import (
    LOAD_EVENT_LOG,
    LOAD_SUMMARY_LOG,
    new_load_observation,
)
from impodo.web.supporting_access_diagnostics import sanitize_supporting_access
from impodo.web.supporting_diagnostics import sanitize_supporting_failure

DIAGNOSTIC_SCHEMA_VERSION = 1
DIAGNOSTIC_DIRECTORY_NAME = "diagnostics"
DIAGNOSTIC_LOG_NAME = "impodo.jsonl"
DIAGNOSTIC_FALLBACK_LOG_NAME = f"{DIAGNOSTIC_LOG_NAME}.concurrent"
DIAGNOSTIC_LOCK_NAME = ".impodo-diagnostics.lock"
DEFAULT_MAX_LOG_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
DEFAULT_SLOW_REQUEST_SECONDS = 2.0
DEFAULT_DIAGNOSTIC_LOCK_TIMEOUT_SECONDS = 0.25
REQUEST_ID_HEADER = "X-Impodo-Request-ID"
DIAGNOSTIC_BUNDLE_SCHEMA_VERSION = 3
DEFAULT_MAX_BUNDLE_RECORDS = 5_000
DEFAULT_MAX_DIAGNOSTIC_LINE_BYTES = 64 * 1024
_MAPPING_ACTIONS = {item.value for item in MappingMutationAction}

_SAFE_SERVER_TIMINGS = frozenset(
    {
        "queue_wait",
        "access_context",
        "workspace_read",
        "owner_read",
        "navigation_read",
        "navigation_present",
        "template_render",
        "view_build",
        "projection",
        "render",
        "summary_context",
        "summary_evidence",
        "summary_execution",
        "summary_quality_page",
        "summary_readiness",
        "summary_render",
        "db_connect",
        "db_schema",
        "db_lock_wait",
        "total",
    }
)
_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DURATION = re.compile(r"(?:^|;)\s*dur=([0-9]+(?:\.[0-9]+)?)\s*(?:;|$)")


def diagnostic_directory(project_root: str | Path) -> Path:
    """Return the protected local directory used for operational evidence."""

    return Path(project_root).resolve().parent / DIAGNOSTIC_DIRECTORY_NAME


class _ConcurrentRotatingFileHandler(RotatingFileHandler):
    """Rotate safely when multiple local Impodo processes share one log.

    Windows does not allow a file to be renamed while another process keeps it
    open. Every current handler therefore coordinates through a lock file and
    closes the JSONL stream after each record. If an older Impodo process still
    owns the primary log, the handler moves to one shared, bounded fallback log
    instead of printing a logging traceback for every request.
    """

    def __init__(
        self,
        filename: str | Path,
        *,
        max_bytes: int,
        backup_count: int,
        encoding: str,
    ) -> None:
        primary_path = Path(filename)
        primary_path.touch(mode=0o600, exist_ok=True)
        fallback_name = (
            DIAGNOSTIC_FALLBACK_LOG_NAME
            if primary_path.name == DIAGNOSTIC_LOG_NAME
            else f"{primary_path.name}.concurrent"
        )
        self._fallback_filename = os.path.abspath(
            primary_path.parent / fallback_name
        )
        lock_name = (
            DIAGNOSTIC_LOCK_NAME
            if primary_path.name == DIAGNOSTIC_LOG_NAME
            else f".{primary_path.name}.lock"
        )
        self.lock_path = primary_path.parent / lock_name
        self._process_lock_stream = None
        self.write_failure_count = 0
        super().__init__(
            primary_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding=encoding,
            delay=True,
        )
        self._primary_filename = self.baseFilename
        try:
            self._process_lock_stream = self.lock_path.open(
                "a+b",
                buffering=0,
            )
            if self.lock_path.stat().st_size == 0:
                self._process_lock_stream.write(b"\0")
                self._process_lock_stream.flush()
        except OSError:
            super().close()
            raise

    def emit(self, record: logging.LogRecord) -> None:
        lock_stream = self._process_lock_stream
        if lock_stream is None:
            return
        try:
            with _exclusive_process_file_lock(lock_stream):
                self._close_output_stream()
                if self.shouldRollover(record):
                    try:
                        self.doRollover()
                    except PermissionError as error:
                        if (
                            self.baseFilename != self._primary_filename
                            or not _is_windows_sharing_violation(error)
                        ):
                            raise
                        self._close_output_stream()
                        self.baseFilename = self._fallback_filename
                logging.FileHandler.emit(self, record)
                if self.stream is not None:
                    self.stream.flush()
                self._apply_private_output_permissions()
        except (OSError, TimeoutError, ValueError):
            # Optional diagnostics must never delay or break the application.
            self.write_failure_count += 1
            return
        finally:
            self._close_output_stream()

    def handleError(self, record: logging.LogRecord) -> None:
        """Keep logging's internal error path off the operator console."""
        self.write_failure_count += 1

    def close(self) -> None:
        self._close_output_stream()
        lock_stream = self._process_lock_stream
        self._process_lock_stream = None
        if lock_stream is not None:
            try:
                lock_stream.close()
            except OSError:
                pass
        super().close()

    def _close_output_stream(self) -> None:
        if self.stream is None:
            return
        try:
            self.stream.close()
        except OSError:
            pass
        finally:
            self.stream = None

    def _apply_private_output_permissions(self) -> None:
        if os.name == "nt":
            return
        try:
            Path(self.baseFilename).chmod(0o600)
        except OSError:
            return


@contextmanager
def _exclusive_process_file_lock(
    stream,
    *,
    timeout_seconds: float = DEFAULT_DIAGNOSTIC_LOCK_TIMEOUT_SECONDS,
):
    """Hold one advisory byte lock across a diagnostic write and rollover."""

    deadline = perf_counter() + timeout_seconds
    if os.name == "nt":
        import msvcrt

        acquired = False
        while not acquired:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError as error:
                if perf_counter() >= deadline:
                    raise TimeoutError("Diagnostic log lock timed out") from error
                sleep(0.01)
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    acquired = False
    while not acquired:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as error:
            if perf_counter() >= deadline:
                raise TimeoutError("Diagnostic log lock timed out") from error
            sleep(0.01)
    try:
        yield
    finally:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _is_windows_sharing_violation(error: PermissionError) -> bool:
    return os.name == "nt" and getattr(error, "winerror", None) in {32, 33}


class LocalDiagnosticRecorder:
    """Write bounded JSON records to one rotating local file."""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_LOG_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        slow_request_seconds: float = DEFAULT_SLOW_REQUEST_SECONDS,
        log_name: str = DIAGNOSTIC_LOG_NAME,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("Diagnostic log size must be positive")
        if backup_count < 1:
            raise ValueError("Diagnostic backup count must be positive")
        if slow_request_seconds <= 0:
            raise ValueError("Slow-request threshold must be positive")
        if log_name != DIAGNOSTIC_LOG_NAME and not re.fullmatch(
            rf"{re.escape(DIAGNOSTIC_LOG_NAME)}\.process-[1-9][0-9]*",
            log_name,
        ) and log_name not in {LOAD_EVENT_LOG, LOAD_SUMMARY_LOG}:
            raise ValueError("Diagnostic log name is invalid")

        resolved_directory = Path(directory)
        resolved_directory.mkdir(parents=True, exist_ok=True)
        self.path = resolved_directory / log_name
        self.slow_request_seconds = float(slow_request_seconds)
        self._lock = Lock()
        self._closed = False
        self._load_recorders: dict[str, LocalDiagnosticRecorder] = {}
        self._capture_setup_failures = 0
        self._logger = logging.getLogger(f"impodo.local_diagnostics.{uuid4()}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = _ConcurrentRotatingFileHandler(
            self.path,
            max_bytes=max_bytes,
            backup_count=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._handler = handler
        self._logger.addHandler(handler)
        self._apply_private_permissions(
            resolved_directory,
            self.path,
            handler.lock_path,
        )

    def record_lifecycle(
        self,
        event: str,
        *,
        port: int | None = None,
        reason: str | None = None,
        exit_code: int | None = None,
        exception_class: str | None = None,
        development_mode: bool | None = None,
        build_version: str | None = None,
        child_process_id: int | None = None,
        restart_attempt: int | None = None,
    ) -> None:
        """Record one process or application transition without free-form text."""

        payload: dict[str, Any] = {
            "event": _safe_name(event, fallback="lifecycle_event"),
            "process_id": os.getpid(),
        }
        if port is not None and 0 < int(port) <= 65_535:
            payload["port"] = int(port)
        if reason is not None:
            payload["reason"] = _safe_name(reason, fallback="unspecified")
        if exit_code is not None:
            payload["exit_code"] = int(exit_code)
        if exception_class is not None:
            payload["exception_class"] = _safe_name(
                exception_class,
                fallback="Exception",
            )
        if development_mode is not None:
            payload["development_mode"] = bool(development_mode)
        if build_version is not None:
            payload["build_version"] = _safe_name(
                build_version,
                fallback="unknown",
            )
        if child_process_id is not None and int(child_process_id) > 0:
            payload["child_process_id"] = int(child_process_id)
        if restart_attempt is not None and int(restart_attempt) >= 0:
            payload["restart_attempt"] = int(restart_attempt)
        self._write(payload)

    def record_request(
        self,
        *,
        request_id: str,
        method: str,
        route_template: str,
        status_code: int,
        duration_ms: float,
        exception_class: str | None = None,
        working_draft_version: int | None = None,
        mapping_operation_id: str | None = None,
        validation_failure: object = None,
        supporting_access: object = None,
        mapping_action: str | None = None,
        server_timings_ms: dict[str, float] | None = None,
        database_connection_count: int | None = None,
        database_schema_check_count: int | None = None,
        database_lock_retry_count: int | None = None,
    ) -> None:
        """Record one request using a route template rather than its raw URL."""

        duration = _safe_duration(duration_ms)
        payload: dict[str, Any] = {
            "event": "request_completed",
            "process_id": os.getpid(),
            "request_id": _safe_request_id(request_id),
            "method": _safe_method(method),
            "route_template": _safe_route_template(route_template),
            "status_code": int(status_code),
            "duration_ms": round(duration, 3),
            "slow": duration >= self.slow_request_seconds * 1000,
        }
        if exception_class is not None:
            payload["exception_class"] = _safe_name(
                exception_class,
                fallback="Exception",
            )
        if working_draft_version is not None:
            payload["working_draft_version"] = max(
                0,
                int(working_draft_version),
            )
        safe_operation_id = _safe_mapping_operation_id(mapping_operation_id)
        if safe_operation_id is not None:
            payload["mapping_operation_id"] = safe_operation_id
        failure = sanitize_supporting_failure(validation_failure)
        if failure is not None:
            payload["validation_failure"] = failure
        access = sanitize_supporting_access(supporting_access)
        if access is not None:
            payload["supporting_access"] = access
        if isinstance(mapping_action, str) and mapping_action in _MAPPING_ACTIONS:
            payload["mapping_action"] = mapping_action
        for name, value in (
            ("database_connection_count", database_connection_count),
            ("database_schema_check_count", database_schema_check_count),
            ("database_lock_retry_count", database_lock_retry_count),
        ):
            if value is not None:
                payload[name] = max(0, int(value))
        timings = _safe_server_timings(server_timings_ms or {})
        if timings:
            payload["server_timings_ms"] = timings
        self._write(payload)

    def record_event_loop_delay(self, *, duration_ms: float) -> None:
        """Record a delayed loop turn without stack, request, or data content."""

        duration = _safe_duration(duration_ms)
        self._write(
            {
                "event": "event_loop_delay_observed",
                "process_id": os.getpid(),
                "duration_ms": round(duration, 3),
                "slow": duration >= self.slow_request_seconds * 1000,
            }
        )

    def record_json2_read_failure(
        self,
        *,
        request_id: str,
        failure: Json2ReadFailureDiagnostic,
    ) -> None:
        """Record one exhausted read using only closed JSON-2 identifiers."""

        self._write(
            {
                "event": "odoo_json2_read_failed",
                "process_id": os.getpid(),
                "request_id": _safe_request_id(request_id),
                "odoo_model": _safe_name(failure.model, fallback="unknown_model"),
                "json2_method": _safe_name(
                    failure.method,
                    fallback="unknown_method",
                ),
                "failure_category": _safe_name(
                    failure.category,
                    fallback="unreachable",
                ),
                "attempt_count": max(1, min(100, int(failure.attempt_count))),
            }
        )

    def record_operation_stage(
        self,
        operation: str,
        stage: str,
        *,
        duration_ms: float,
        outcome: str = "completed",
        reason: str | None = None,
        exception_class: str | None = None,
        completed_rows: int | None = None,
        total_rows: int | None = None,
        pass_number: int | None = None,
        total_record_types: int | None = None,
        new_record_types: int | None = None,
        odoo_model: str | None = None,
        json2_method: str | None = None,
        odoo_fields: tuple[str, ...] = (),
        odoo_status_code: int | None = None,
        odoo_error_type: OdooWriteErrorType | None = None,
    ) -> None:
        """Record one bounded application stage without business identifiers."""

        duration = _safe_duration(duration_ms)
        payload: dict[str, Any] = {
            "event": "operation_stage_completed",
            "process_id": os.getpid(),
            "operation": _safe_name(operation, fallback="operation"),
            "stage": _safe_name(stage, fallback="stage"),
            "outcome": _safe_name(outcome, fallback="completed"),
            "duration_ms": round(duration, 3),
            "slow": duration >= self.slow_request_seconds * 1000,
        }
        if reason is not None:
            payload["reason"] = _safe_name(reason, fallback="unspecified")
        if exception_class is not None:
            payload["exception_class"] = _safe_name(
                exception_class,
                fallback="Exception",
            )
        if completed_rows is not None:
            payload["completed_rows"] = max(0, int(completed_rows))
        if total_rows is not None:
            payload["total_rows"] = max(0, int(total_rows))
        if pass_number is not None:
            payload["pass_number"] = max(0, min(100, int(pass_number)))
        if total_record_types is not None:
            payload["total_record_types"] = max(
                0, min(10_000, int(total_record_types))
            )
        if new_record_types is not None:
            payload["new_record_types"] = max(
                0, min(10_000, int(new_record_types))
            )
        if operation == "odoo_load" and stage == "write_result":
            payload.update(_safe_write_result({
                "odoo_model": odoo_model,
                "json2_method": json2_method,
                "odoo_fields": list(odoo_fields),
                "odoo_status_code": odoo_status_code,
                "odoo_error_type": odoo_error_type,
            }))
        self._write(payload)

    def record_load_control(self, control: LoadControl) -> None:
        """Retain bounded policy, pause limits and counts without row evidence."""
        payload = json.loads(control.to_json())
        payload.pop("windows")
        payload["failure_windows"] = [
            {"model": window.model, "operation": window.operation,
             "consecutive_rejections": window.consecutive,
             "settled_count": len(window.recent), "rejected_count": sum(window.recent)}
            for window in control.windows[:20]
        ]
        payload["omitted_window_count"] = max(0, len(control.windows) - 20)
        self._write({"event": "odoo_load_control", **payload})

    def close(self) -> None:
        """Flush and close the rotating file without writing another record."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._handler.flush()
            self._handler.close()
            self._logger.removeHandler(self._handler)
            for recorder in self._load_recorders.values():
                recorder.close()

    def flush(self) -> None:
        """Flush current records so an exported bundle sees completed writes."""

        with self._lock:
            if self._closed:
                return
            try:
                self._handler.flush()
                for recorder in self._load_recorders.values():
                    recorder.flush()
            except (OSError, ValueError):
                return

    def load_observation(self, *, action: str, request_id: str):
        """Open independent bounded histories for load events and summaries."""
        with self._lock:
            try:
                for name in (LOAD_EVENT_LOG, LOAD_SUMMARY_LOG):
                    if name not in self._load_recorders:
                        self._load_recorders[name] = LocalDiagnosticRecorder(self.path.parent, log_name=name)
            except (OSError, ValueError):
                self._capture_setup_failures += 1
                raise
        return new_load_observation(
            self._load_recorders[LOAD_EVENT_LOG]._write,
            self._load_recorders[LOAD_SUMMARY_LOG]._write,
            action=action, request_id=request_id,
        )

    @property
    def capture_failure_count(self) -> int:
        """Report observed storage failures without masking their coverage gap."""
        return self._capture_setup_failures + self._handler.write_failure_count + sum(
            recorder.capture_failure_count for recorder in self._load_recorders.values()
        )

    def _write(self, payload: dict[str, Any]) -> None:
        envelope = {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "recorded_at": datetime.now(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
            **payload,
        }
        encoded = json.dumps(
            envelope,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock:
            if self._closed:
                return
            try:
                self._logger.info(encoded)
                self._handler.flush()
            except (OSError, ValueError):
                # Diagnostics must never make the local application unavailable.
                self._handler.write_failure_count += 1
                return

    @staticmethod
    def _apply_private_permissions(directory: Path, *paths: Path) -> None:
        if os.name == "nt":
            # The launcher prepares this directory with the private-root policy.
            return
        try:
            directory.chmod(0o700)
            for path in paths:
                path.chmod(0o600)
        except OSError:
            # A restrictive process umask remains the fallback on unusual filesystems.
            return


class RequestDiagnosticsMiddleware:
    """Attach a request identity and record one bounded terminal request fact."""

    def __init__(self, app, *, recorder: LocalDiagnosticRecorder) -> None:
        self.app = app
        self.recorder = recorder

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid4())
        state = scope.setdefault("state", {})
        state["diagnostic_request_id"] = request_id
        observer = None
        if scope.get("method") == "POST":
            match = re.fullmatch(r"/workspaces/[^/]+/(load|transfer-load)(/recover|/reconcile|/reverify|/recovery/check|/stop)?", str(scope.get("path", "")))
            if match is not None:
                action = {None: "LOAD", "/recover": "RECOVER", "/reconcile": "VERIFY", "/reverify": "VERIFY", "/recovery/check": "CHECK_RECOVERY", "/stop": "STOP"}[match.group(2)]
                if match.group(1) == "transfer-load":
                    action = "TRANSFER_" + action
                try:
                    observer = self.recorder.load_observation(action=action, request_id=request_id)
                    state["diagnostic_attempt_id"] = observer.attempt_id
                except (OSError, ValueError):
                    self.recorder.record_lifecycle("load_diagnostic_capture_failed")
        started = perf_counter()
        status_code = 500
        exception_class: str | None = None
        server_timings_ms: dict[str, float] = {}

        def record_json2_failure(failure: Json2ReadFailureDiagnostic) -> None:
            self.recorder.record_json2_read_failure(
                request_id=request_id,
                failure=failure,
            )

        async def diagnostic_send(message) -> None:
            nonlocal status_code, server_timings_ms
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                if observer is not None:
                    headers["X-Impodo-Support-Reference"] = observer.attempt_id
                database_header = ", ".join(
                    f"{name};dur={duration:.3f}"
                    for name, duration in database_timings.server_timings_ms().items()
                )
                if database_header:
                    existing = headers.get("server-timing", "")
                    headers["Server-Timing"] = (
                        f"{existing}, {database_header}"
                        if existing
                        else database_header
                    )
                server_timings_ms = parse_server_timing(
                    headers.get("server-timing", "")
                )
            await send(message)

        with (
            observe_json2_read_failures(record_json2_failure),
            collect_duckdb_request_timings() as database_timings,
            bind_load_observation(observer),
        ):
            try:
                await self.app(scope, receive, diagnostic_send)
            except BaseException as error:
                exception_class = type(error).__name__
                record_load_failure(error)
                raise
            finally:
                if observer is not None and not observer.enqueued and not observer.terminal:
                    observer.terminal = True
                    observer.emit("terminal", outcome="FAILED" if status_code >= 400 else "COMPLETED",
                        reason="SUBMISSION_REJECTED" if status_code >= 400 else "")
                route = scope.get("route")
                route_template = getattr(route, "path", "<unmatched>")
                self.recorder.record_request(
                    request_id=request_id,
                    method=str(scope.get("method", "UNKNOWN")),
                    route_template=str(route_template),
                    status_code=status_code,
                    duration_ms=(perf_counter() - started) * 1000,
                    exception_class=exception_class,
                    working_draft_version=_optional_nonnegative_int(
                        state.get("diagnostic_working_draft_version")
                    ),
                    mapping_operation_id=state.get("diagnostic_mapping_operation_id"),
                    validation_failure=state.get("diagnostic_validation_failure"),
                    supporting_access=state.get("diagnostic_supporting_access"),
                    mapping_action=state.get("diagnostic_mapping_action"),
                    server_timings_ms=server_timings_ms,
                    database_connection_count=(
                        database_timings.connection_count
                    ),
                    database_schema_check_count=(
                        database_timings.schema_check_count
                    ),
                    database_lock_retry_count=(
                        database_timings.lock_retry_count
                    ),
                )


def set_diagnostic_working_draft_version(request, version: int | None) -> None:
    """Expose only a safe version number to request diagnostics."""

    if version is None:
        return
    request.state.diagnostic_working_draft_version = max(0, int(version))


def set_diagnostic_mapping_operation_id(request, operation_id: str) -> None:
    """Correlate the matching support reference using only its validated UUID."""
    request.state.diagnostic_mapping_operation_id = _safe_mapping_operation_id(operation_id)


def _safe_mapping_operation_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def create_diagnostic_bundle(
    directory: str | Path,
    *,
    build_contract: ApplicationBuildContract,
    max_records: int = DEFAULT_MAX_BUNDLE_RECORDS,
    attempt_id: str | None = None,
    capture_failure_count: int = 0,
) -> bytes:
    """Create a bounded ZIP whose contents cannot carry business data."""

    if max_records < 1:
        raise ValueError("Diagnostic bundle record limit must be positive")
    records: list[dict[str, Any]] = []
    input_issues = {"malformed_record_count": 0, "oversize_record_count": 0, "unreadable_file_count": 0}
    for path in sorted(Path(directory).glob(f"{DIAGNOSTIC_LOG_NAME}*")):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                for raw_line in stream:
                    if len(raw_line) > DEFAULT_MAX_DIAGNOSTIC_LINE_BYTES:
                        input_issues["oversize_record_count"] += 1
                        continue
                    try:
                        candidate = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        input_issues["malformed_record_count"] += 1
                        continue
                    safe = _sanitize_diagnostic_record(candidate)
                    if safe is not None:
                        records.append(safe)
                    else:
                        input_issues["malformed_record_count"] += 1
        except OSError:
            input_issues["unreadable_file_count"] += 1
            continue

    records.sort(key=lambda item: str(item.get("recorded_at", "")))
    supporting_access_records = [
        item for item in records
        if item.get("supporting_access")
        or item.get("validation_failure", {}).get("stage") == "supporting_review"
    ][-100:]
    failure_records = [
        item for item in records
        if item.get("validation_failure")
        or item.get("supporting_access", {}).get("outcome") in {
            "REVIEW_REQUIRED", "SCHEMA_REVIEW_REQUIRED", "REFRESH_FAILED",
        }
        or item.get("exception_class")
        or item.get("event") == "odoo_json2_read_failed"
        or (item.get("event") == "odoo_load_control" and item.get("state") in {"PAUSED", "STOPPED"})
        or (item.get("event") == "operation_stage_completed"
            and item.get("operation") == "odoo_load" and item.get("stage") == "write_result")
        or (isinstance(item.get("status_code"), int) and item["status_code"] >= 400)
    ][-100:]
    available_record_count = len(records)
    records = records[-max_records:]
    slow_records = [
        item
        for item in records
        if item.get("slow") is True
        and item.get("event")
        in {
            "request_completed",
            "event_loop_delay_observed",
            "operation_stage_completed",
        }
    ][-100:]
    load_members, load_coverage = load_bundle_members(directory, failure_records, attempt_id=attempt_id)
    created_at = _utc_timestamp()
    manifest = {
        "diagnostic_bundle_schema_version": DIAGNOSTIC_BUNDLE_SCHEMA_VERSION,
        "created_at": created_at,
        "application": {
            "build_id": build_contract.application_build_id,
            "build_contract_version": build_contract.contract_version,
            "workspace_schema_generation": (
                build_contract.workspace_schema_generation
            ),
            "workspace_schema_version": build_contract.workspace_schema_version,
        },
        "runtime": {
            "operating_system": platform.system() or "Unknown",
            "python_version": (
                f"{sys.version_info.major}.{sys.version_info.minor}."
                f"{sys.version_info.micro}"
            ),
        },
        "record_count": len(records),
        "slow_record_count": len(slow_records),
        "failure_record_count": len(failure_records),
        "supporting_access_record_count": len(supporting_access_records),
        "load_evidence": load_coverage,
        "recording_failure_count_current_session": max(0, capture_failure_count),
        "coverage": {
            **input_issues,
            "general": {"first_recorded_at": records[0]["recorded_at"] if records else None,
                "last_recorded_at": records[-1]["recorded_at"] if records else None,
                "omitted_record_count": max(0, available_record_count - len(records)), "max_records": max_records},
            "failures": {"first_recorded_at": failure_records[0]["recorded_at"] if failure_records else None,
                "last_recorded_at": failure_records[-1]["recorded_at"] if failure_records else None, "max_records": 100},
            "history_before_retained_logs": "unavailable; rotated records may have expired",
        },
        "privacy": (
            "Operational and technical schema metadata only, including model and field identifiers. "
            "Source rows, formulas, credentials, "
            "tokens, request bodies, headers, URLs, and query strings are omitted."
        ),
    }
    encoded_records = "".join(
        json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
        for item in records
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in load_members.items():
            archive.writestr(name, content)
        archive.writestr(
            "supporting-access.json",
            json.dumps(supporting_access_records, ensure_ascii=True, sort_keys=True, indent=2),
        )
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True)
            + "\n",
        )
        archive.writestr("diagnostics.jsonl", encoded_records)
        archive.writestr(
            "recent-failures.json",
            json.dumps(failure_records, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        )
        archive.writestr(
            "slow-requests.json",
            json.dumps(
                slow_records,
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
    return buffer.getvalue()


def parse_server_timing(value: str) -> dict[str, float]:
    """Read only Impodo's allowlisted numeric Server-Timing phases."""

    timings: dict[str, float] = {}
    for raw_metric in value.split(","):
        name, separator, parameters = raw_metric.strip().partition(";")
        if not separator or name not in _SAFE_SERVER_TIMINGS:
            continue
        match = _DURATION.search(f";{parameters}")
        if match is None:
            continue
        duration = float(match.group(1))
        if 0 <= duration <= 86_400_000:
            timings[name] = round(duration, 3)
    return timings


async def monitor_event_loop(
    recorder: LocalDiagnosticRecorder,
    *,
    interval_seconds: float = 0.5,
    delay_threshold_seconds: float | None = None,
) -> None:
    """Record bounded evidence when synchronous work delays the event loop."""

    if interval_seconds <= 0:
        raise ValueError("Event-loop monitor interval must be positive")
    threshold = (
        recorder.slow_request_seconds
        if delay_threshold_seconds is None
        else float(delay_threshold_seconds)
    )
    if threshold <= 0:
        raise ValueError("Event-loop delay threshold must be positive")

    loop = asyncio.get_running_loop()
    expected_tick = loop.time() + interval_seconds
    while True:
        await asyncio.sleep(interval_seconds)
        observed_at = loop.time()
        delay = max(0.0, observed_at - expected_tick)
        if delay >= threshold:
            recorder.record_event_loop_delay(duration_ms=delay * 1000)
        expected_tick = observed_at + interval_seconds


def install_asyncio_exception_diagnostics(
    loop,
    recorder: LocalDiagnosticRecorder | None,
    *,
    platform_name: str | None = None,
):
    """Install a narrow transport-noise filter and return the prior handler."""

    previous_handler = loop.get_exception_handler()
    resolved_platform = platform_name or sys.platform

    def handle_exception(active_loop, context: dict[str, Any]) -> None:
        if _is_expected_windows_proactor_reset(
            context,
            platform_name=resolved_platform,
        ):
            if recorder is not None:
                recorder.record_lifecycle(
                    "client_connection_reset",
                    reason="windows_proactor_cleanup",
                    exception_class="ConnectionResetError",
                )
            return
        if previous_handler is not None:
            previous_handler(active_loop, context)
        else:
            active_loop.default_exception_handler(context)

    loop.set_exception_handler(handle_exception)
    return previous_handler


def _is_expected_windows_proactor_reset(
    context: dict[str, Any],
    *,
    platform_name: str,
) -> bool:
    """Match only CPython's known Windows connection-close callback noise."""

    exception = context.get("exception")
    error_number = getattr(exception, "winerror", None)
    message = str(context.get("message", ""))
    return (
        platform_name == "win32"
        and isinstance(exception, ConnectionResetError)
        and error_number == 10054
        and "_ProactorBasePipeTransport._call_connection_lost" in message
    )


def _safe_server_timings(values: dict[str, float]) -> dict[str, float]:
    timings: dict[str, float] = {}
    for key, value in sorted(values.items()):
        if key not in _SAFE_SERVER_TIMINGS:
            continue
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and 0 <= duration <= 86_400_000:
            timings[key] = round(duration, 3)
    return timings


def _safe_duration(value: Any) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(duration) or duration < 0:
        return 0.0
    return min(duration, 86_400_000.0)


def _safe_name(value: str, *, fallback: str) -> str:
    candidate = str(value).strip()
    return candidate if _SAFE_NAME.fullmatch(candidate) else fallback


def _safe_request_id(value: str) -> str:
    candidate = str(value).strip()
    return (
        candidate
        if _SAFE_REQUEST_ID.fullmatch(candidate)
        else "invalid-request-id"
    )


def _safe_method(value: str) -> str:
    candidate = str(value).upper()
    allowed = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
    return candidate if candidate in allowed else "UNKNOWN"


def _safe_route_template(value: str) -> str:
    candidate = str(value)
    if (
        not candidate.startswith("/")
        or "?" in candidate
        or "#" in candidate
        or len(candidate) > 256
    ):
        return "<unmatched>"
    segments = candidate.strip("/").split("/") if candidate != "/" else []
    for segment in segments:
        if segment.startswith("{") and segment.endswith("}"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9._-]+", segment):
            return "<unmatched>"
    return candidate


def _optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _sanitize_diagnostic_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    event = _safe_name(str(value.get("event", "")), fallback="")
    recorded_at = str(value.get("recorded_at", ""))
    if not event or not re.fullmatch(r"[0-9T:.+Z-]{10,40}", recorded_at):
        return None
    safe: dict[str, Any] = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "recorded_at": recorded_at,
        "event": event,
    }
    if event == "odoo_load_control":
        safe.update(_sanitize_load_control(value))
    write_result = (
        event == "operation_stage_completed"
        and value.get("operation") == "odoo_load"
        and value.get("stage") == "write_result"
    )
    if write_result:
        safe.update(_safe_write_result(value))
    integer_fields = {
        "process_id": (1, 2_147_483_647),
        "child_process_id": (1, 2_147_483_647),
        "port": (1, 65_535),
        "exit_code": (-2_147_483_648, 2_147_483_647),
        "restart_attempt": (0, 100),
        "status_code": (100, 599),
        "working_draft_version": (0, 9_223_372_036_854_775_807),
        "database_connection_count": (0, 1_000_000),
        "database_schema_check_count": (0, 1_000_000),
        "database_lock_retry_count": (0, 1_000_000),
        "completed_rows": (0, 9_223_372_036_854_775_807),
        "total_rows": (0, 9_223_372_036_854_775_807),
        "pass_number": (0, 100),
        "total_record_types": (0, 10_000),
        "new_record_types": (0, 10_000),
        "attempt_count": (1, 100),
    }
    for field, (minimum, maximum) in integer_fields.items():
        parsed = _bounded_int(value.get(field), minimum, maximum)
        if parsed is not None:
            safe[field] = parsed
    for field in (
        "reason",
        "exception_class",
        "build_version",
        "operation",
        "stage",
        "outcome",
        "odoo_model",
        "json2_method",
        "failure_category",
    ):
        if field in value and not (write_result and field in {"odoo_model", "json2_method"}):
            candidate = _safe_name(str(value[field]), fallback="")
            if candidate:
                safe[field] = candidate
    if "request_id" in value:
        safe["request_id"] = _safe_request_id(str(value["request_id"]))
    operation_id = _safe_mapping_operation_id(value.get("mapping_operation_id"))
    if operation_id is not None:
        safe["mapping_operation_id"] = operation_id
    failure = sanitize_supporting_failure(value.get("validation_failure"))
    if failure is not None:
        safe["validation_failure"] = failure
    access = sanitize_supporting_access(value.get("supporting_access"))
    if access is not None:
        safe["supporting_access"] = access
    if isinstance(value.get("mapping_action"), str) and value["mapping_action"] in _MAPPING_ACTIONS:
        safe["mapping_action"] = value["mapping_action"]
    if "method" in value:
        safe["method"] = _safe_method(str(value["method"]))
    if "route_template" in value:
        safe["route_class"] = _diagnostic_route_class(
            str(value["route_template"])
        )
    for field in ("duration_ms",):
        if field in value:
            safe[field] = round(_safe_duration(value[field]), 3)
    for field in ("slow", "development_mode"):
        if isinstance(value.get(field), bool):
            safe[field] = value[field]
    timings = value.get("server_timings_ms")
    if isinstance(timings, dict):
        safe_timings = _safe_server_timings(timings)
        if safe_timings:
            safe["server_timings_ms"] = safe_timings
    return safe


def _safe_write_result(value: dict[str, Any]) -> dict[str, object]:
    """Keep bounded Odoo write metadata at both recording and export."""
    result: dict[str, object] = {}
    model = value.get("odoo_model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", model):
        result["odoo_model"] = model
    method = value.get("json2_method")
    if isinstance(method, str) and method in {"load", "create", "write", "search_read", "completion"}:
        result["json2_method"] = method
    fields = value.get("odoo_fields")
    if isinstance(fields, list):
        safe_fields = [
            field for field in fields[:20]
            if isinstance(field, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", field)
        ]
        if safe_fields:
            result["odoo_fields"] = safe_fields
    status = _bounded_int(value.get("odoo_status_code"), 100, 599)
    if status is not None:
        result["odoo_status_code"] = status
    error_type = value.get("odoo_error_type")
    if isinstance(error_type, str) and error_type in {item.value for item in OdooWriteErrorType}:
        result["odoo_error_type"] = error_type
    return result


def _sanitize_load_control(value: dict[str, Any]) -> dict[str, Any]:
    """Keep validated pause limits and counts without copying row evidence."""
    keys = (
        "policy", "state", "reason", "consecutive_limit", "window_size",
        "minimum_results", "rejection_percent", "isolation_limit",
        "isolation_calls", "contract_version",
    )
    try:
        control = LoadControl(**{key: value[key] for key in keys})
    except (KeyError, TypeError, ValueError):
        return {}
    result = json.loads(control.to_json())
    result.pop("windows")
    result.pop("content_hash")
    windows = []
    raw_windows = value.get("failure_windows")
    for raw in raw_windows[:20] if isinstance(raw_windows, list) else ():
        if not isinstance(raw, dict):
            continue
        try:
            window = FailureWindow(
                raw["model"], raw["operation"], raw["consecutive_rejections"]
            )
            settled, rejected = raw["settled_count"], raw["rejected_count"]
            if (
                type(settled) is not int or type(rejected) is not int
                or not 0 <= rejected <= settled <= control.window_size
            ):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        windows.append({
            "model": window.model, "operation": window.operation,
            "consecutive_rejections": window.consecutive,
            "settled_count": settled, "rejected_count": rejected,
        })
    result["failure_windows"] = windows
    omitted = value.get("omitted_window_count")
    if type(omitted) is int and 0 <= omitted <= 1_000:
        result["omitted_window_count"] = omitted
    return result


def _bounded_int(value: Any, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if minimum <= parsed <= maximum else None


def _diagnostic_route_class(value: str) -> str:
    route = _safe_route_template(value)
    if route == "<unmatched>":
        return "unmatched"
    segments = route.strip("/").split("/") if route != "/" else []
    if not segments:
        return "root"
    if segments[0] == "workspaces":
        area = segments[2] if len(segments) >= 3 else "workspace"
        allowed_areas = {
            "correction",
            "derived-entities",
            "load",
            "mapping",
            "normalization",
            "overview",
            "preparation",
            "preflight",
            "quality",
            "resolution",
            "schema",
            "sources",
            "summary",
            "target",
            "transformation-impact",
        }
        return area if area in allowed_areas else "workspace"
    allowed_roots = {
        "concepts",
        "diagnostics",
        "health",
        "launch",
        "projects",
        "quit",
        "static",
    }
    return segments[0] if segments[0] in allowed_roots else "other"


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
