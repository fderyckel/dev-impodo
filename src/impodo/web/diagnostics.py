"""Privacy-safe local diagnostics for the loopback browser application.

The recorder accepts only bounded operational fields. Request URLs, query
strings, headers, bodies, source values, formulas, credentials, and tokens are
deliberately outside its API so routine diagnostics cannot capture them by
accident.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import BytesIO
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import platform
import re
import sys
from threading import Lock
from time import perf_counter
from typing import Any
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from starlette.datastructures import MutableHeaders

from impodo.application.shared.build_contract import ApplicationBuildContract


DIAGNOSTIC_SCHEMA_VERSION = 1
DIAGNOSTIC_DIRECTORY_NAME = "diagnostics"
DIAGNOSTIC_LOG_NAME = "impodo.jsonl"
DEFAULT_MAX_LOG_BYTES = 2 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5
DEFAULT_SLOW_REQUEST_SECONDS = 2.0
REQUEST_ID_HEADER = "X-Impodo-Request-ID"
DIAGNOSTIC_BUNDLE_SCHEMA_VERSION = 1
DEFAULT_MAX_BUNDLE_RECORDS = 5_000
DEFAULT_MAX_DIAGNOSTIC_LINE_BYTES = 64 * 1024

_SAFE_SERVER_TIMINGS = frozenset(
    {
        "queue_wait",
        "workspace_read",
        "view_build",
        "projection",
        "render",
        "total",
    }
)
_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_DURATION = re.compile(r"(?:^|;)\s*dur=([0-9]+(?:\.[0-9]+)?)\s*(?:;|$)")


def diagnostic_directory(project_root: str | Path) -> Path:
    """Return the protected local directory used for operational evidence."""

    return Path(project_root).resolve().parent / DIAGNOSTIC_DIRECTORY_NAME


class LocalDiagnosticRecorder:
    """Write bounded JSON records to one rotating local file."""

    def __init__(
        self,
        directory: str | Path,
        *,
        max_bytes: int = DEFAULT_MAX_LOG_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        slow_request_seconds: float = DEFAULT_SLOW_REQUEST_SECONDS,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("Diagnostic log size must be positive")
        if backup_count < 1:
            raise ValueError("Diagnostic backup count must be positive")
        if slow_request_seconds <= 0:
            raise ValueError("Slow-request threshold must be positive")

        resolved_directory = Path(directory)
        resolved_directory.mkdir(parents=True, exist_ok=True)
        self.path = resolved_directory / DIAGNOSTIC_LOG_NAME
        self.slow_request_seconds = float(slow_request_seconds)
        self._lock = Lock()
        self._closed = False
        self._logger = logging.Logger(
            f"impodo.local_diagnostics.{uuid4()}",
            level=logging.INFO,
        )
        self._logger.propagate = False
        handler = RotatingFileHandler(
            self.path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._handler = handler
        self._logger.addHandler(handler)
        self._apply_private_permissions(resolved_directory, self.path)

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
        server_timings_ms: dict[str, float] | None = None,
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

    def record_operation_stage(
        self,
        operation: str,
        stage: str,
        *,
        duration_ms: float,
        outcome: str = "completed",
        reason: str | None = None,
        exception_class: str | None = None,
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
        self._write(payload)

    def close(self) -> None:
        """Flush and close the rotating file without writing another record."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._handler.flush()
            self._handler.close()
            self._logger.removeHandler(self._handler)

    def flush(self) -> None:
        """Flush current records so an exported bundle sees completed writes."""

        with self._lock:
            if self._closed:
                return
            try:
                self._handler.flush()
            except (OSError, ValueError):
                return

    def _write(self, payload: dict[str, Any]) -> None:
        envelope = {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "recorded_at": datetime.now(timezone.utc)
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
                return

    @staticmethod
    def _apply_private_permissions(directory: Path, path: Path) -> None:
        if os.name == "nt":
            # The launcher prepares this directory with the private-root policy.
            return
        try:
            directory.chmod(0o700)
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
        started = perf_counter()
        status_code = 500
        exception_class: str | None = None
        server_timings_ms: dict[str, float] = {}

        async def diagnostic_send(message) -> None:
            nonlocal status_code, server_timings_ms
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                server_timings_ms = parse_server_timing(
                    headers.get("server-timing", "")
                )
            await send(message)

        try:
            await self.app(scope, receive, diagnostic_send)
        except BaseException as error:
            exception_class = type(error).__name__
            raise
        finally:
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
                server_timings_ms=server_timings_ms,
            )


def set_diagnostic_working_draft_version(request, version: int | None) -> None:
    """Expose only a safe version number to request diagnostics."""

    if version is None:
        return
    request.state.diagnostic_working_draft_version = max(0, int(version))


def create_diagnostic_bundle(
    directory: str | Path,
    *,
    build_contract: ApplicationBuildContract,
    max_records: int = DEFAULT_MAX_BUNDLE_RECORDS,
) -> bytes:
    """Create a bounded ZIP whose contents cannot carry business data."""

    if max_records < 1:
        raise ValueError("Diagnostic bundle record limit must be positive")
    records: list[dict[str, Any]] = []
    for path in sorted(Path(directory).glob(f"{DIAGNOSTIC_LOG_NAME}*")):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                for raw_line in stream:
                    if len(raw_line) > DEFAULT_MAX_DIAGNOSTIC_LINE_BYTES:
                        continue
                    try:
                        candidate = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    safe = _sanitize_diagnostic_record(candidate)
                    if safe is not None:
                        records.append(safe)
        except OSError:
            continue

    records.sort(key=lambda item: str(item.get("recorded_at", "")))
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
        "privacy": (
            "Operational metadata only. Source rows, formulas, credentials, "
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
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True)
            + "\n",
        )
        archive.writestr("diagnostics.jsonl", encoded_records)
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
    integer_fields = {
        "process_id": (1, 2_147_483_647),
        "child_process_id": (1, 2_147_483_647),
        "port": (1, 65_535),
        "exit_code": (-2_147_483_648, 2_147_483_647),
        "restart_attempt": (0, 100),
        "status_code": (100, 599),
        "working_draft_version": (0, 9_223_372_036_854_775_807),
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
    ):
        if field in value:
            candidate = _safe_name(str(value[field]), fallback="")
            if candidate:
                safe[field] = candidate
    if "request_id" in value:
        safe["request_id"] = _safe_request_id(str(value["request_id"]))
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
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
