"""Allowlist execution-time preparation measurements for capture and export.

This closed schema accepts counts, runtime facts and opaque attempt identities.
It cannot carry source values, file paths, host names or arbitrary environment.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import cast
from uuid import UUID

from .performance_attribution import sanitize_file_io, sanitize_numeric_validation
from .preparation_route_diagnostics import sanitize_dataset_workload

PERFORMANCE_SCHEMA_VERSION = 1
PERFORMANCE_EVENT = "operation_performance"
PERFORMANCE_EVENT_KINDS = frozenset(
    {"attempt_started", "phase_completed", "attempt_finished"}
)
_PHASES = frozenset(
    {
        "QUEUED",
        "VALIDATING",
        "TRANSFORMING",
        "PUBLISHING",
        "QUALITY",
        "NORMALIZING",
        "COMPLETE",
        "EVALUATING",
        "STAGING",
        "queue_wait",
        "worker_startup",
        "process_start_call",
        "worker_first_message_wait",
        "worker_build_hash",
        "worker_runtime_initialization",
        "worker_composition",
        "total",
        "resolution_evaluation",
        "quality_evaluation",
        "quality_persistence_and_hash",
        "normalization_aggregation",
        "normalization_effect_aggregation",
        "normalization_eligible_hash",
        "normalization_persistence_and_hash",
        "source_verification",
        "transformation",
        "relationships",
        "publication",
        "initialization",
    }
)
_OUTCOMES = frozenset(
    {
        "completed",
        "succeeded",
        "failed",
        "cancelled",
        "review_required",
        "interrupted",
        "crashed",
        "started",
    }
)
_ROUTES = frozenset(
    {
        "NATIVE_COLUMNAR",
        "BOUNDED_PYTHON",
        "MIXED_BOUNDED",
        "BOUNDED_DURABLE",
        "BOUNDED_RUNTIME_GUARDED",
        "MATERIALIZING",
        "DEFERRED",
        "BOUNDED",
        "MIXED",
        "UNKNOWN",
    }
)
_VERSION = re.compile(
    r"[0-9]{1,9}(?:\.[0-9]{1,9}){0,3}(?:[a-zA-Z][a-zA-Z0-9.+-]{0,20})?(?:[-+][a-zA-Z0-9.-]{1,24})?"
)
_MAX_INTEGER = 9_223_372_036_854_775_807


def sanitize_performance_event(value: object) -> dict[str, object] | None:
    """Return a closed-schema payload or reject an invalid event identity."""
    raw = _mapping(value)
    if (
        type(raw.get("schema_version")) is not int
        or raw["schema_version"] != PERFORMANCE_SCHEMA_VERSION
    ):
        return None
    attempt_id = _uuid(raw.get("attempt_id"))
    event_kind = _enum(raw.get("event_kind"), PERFORMANCE_EVENT_KINDS)
    operation = _enum(raw.get("operation"), frozenset({"preparation"}))
    if attempt_id is None or event_kind is None or operation is None:
        return None
    result: dict[str, object] = {
        "schema_version": PERFORMANCE_SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "event_kind": event_kind,
        "operation": operation,
    }
    _integers(raw, result, {"sequence": (0, 1_000_000)})
    _numbers(raw, result, {"duration_ms": 86_400_000.0})
    for name, permitted in (
        ("phase", _PHASES),
        ("outcome", _OUTCOMES),
        ("timing_scope", frozenset({"inclusive", "exclusive", "nested"})),
        (
            "failure_reason",
            frozenset(
                {
                    "THREAD_CONFIGURATION_INVALID",
                    "THREAD_CONFIGURATION_MISMATCH",
                    "WORKER_START_FAILED",
                    "WORKER_EXITED",
                    "BUILD_CHANGED",
                    "CANCELLED",
                    "PREPARATION_FAILED",
                    "DIAGNOSTIC_COLLECTION_FAILED",
                    "UNKNOWN",
                }
            ),
        ),
    ):
        if (candidate := _enum(raw.get(name), permitted)) is not None:
            result[name] = candidate
    if event_kind == "phase_completed" and (
        "phase" not in result or "duration_ms" not in result
    ):
        return None
    if type(raw.get("measurement_complete")) is bool:
        result["measurement_complete"] = raw["measurement_complete"]
    for section, sanitizer in (
        ("environment", _environment),
        ("configuration", _configuration),
        ("workload", _workload),
        ("resources", _resources),
        ("database", _database),
        ("file_io", sanitize_file_io),
        ("numeric_validation", sanitize_numeric_validation),
    ):
        if section in raw:
            result[section] = sanitizer(_mapping(raw[section]))
    return result


def _environment(raw: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name in ("python_version", "polars_version", "duckdb_version", "os_version"):
        candidate = raw.get(name)
        if isinstance(candidate, str) and _VERSION.fullmatch(candidate):
            result[name] = candidate
    for name, permitted in (
        ("operating_system", frozenset({"Windows", "Darwin", "Linux", "Unknown"})),
        (
            "architecture",
            frozenset(
                {
                    "arm64",
                    "ARM64",
                    "aarch64",
                    "x86_64",
                    "AMD64",
                    "amd64",
                    "i386",
                    "i686",
                    "x86",
                    "unknown",
                }
            ),
        ),
        ("python_implementation", frozenset({"CPython", "PyPy", "unknown"})),
    ):
        if (candidate := _enum(raw.get(name), permitted)) is not None:
            result[name] = candidate
    build_id = raw.get("build_id")
    if isinstance(build_id, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", build_id):
        result["build_id"] = build_id
    if type(raw.get("odoo_major")) is int and raw["odoo_major"] in {19, 20}:
        result["odoo_major"] = raw["odoo_major"]
    _integers(
        raw,
        result,
        {
            "available_cpu_count": (1, 65_536),
            "logical_cpu_count": (1, 65_536),
            "total_memory_bytes": (1, _MAX_INTEGER),
            "available_memory_bytes": (0, _MAX_INTEGER),
        },
    )
    return result


def _configuration(raw: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    _integers(
        raw,
        result,
        {
            "polars_threads_requested": (1, 65_536),
            "polars_threads_effective": (1, 65_536),
            "duckdb_threads": (1, 65_536),
            "duckdb_threads_max": (1, 65_536),
            "worker_processes": (1, 64),
        },
    )
    source = raw.get("source")
    sources = {
        "PREPARATION_OVERRIDE": "PREPARATION_OVERRIDE",
        "POLARS_OVERRIDE": "POLARS_OVERRIDE",
        "INHERITED_POLARS": "INHERITED_POLARS",
        "DEFAULT": "DEFAULT",
        "preparation_override": "PREPARATION_OVERRIDE",
        "inherited_polars": "INHERITED_POLARS",
        "default": "DEFAULT",
    }
    if isinstance(source, str) and source in sources:
        result["source"] = sources[source]
    if raw.get("duckdb_configuration") == "UNCHANGED":
        result["duckdb_configuration"] = "UNCHANGED"
    return result


def _workload(raw: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    _integers(
        raw,
        result,
        {
            name: (0, _MAX_INTEGER)
            for name in (
                "row_count",
                "column_count",
                "input_bytes",
                "relationship_count",
                "dataset_count",
                "mapped_field_count",
                "completed_rows",
            )
        },
    )
    if (route := _enum(raw.get("processing_route"), _ROUTES)) is not None:
        result["processing_route"] = route
    routes = _mapping(raw.get("stage_routes"))
    permitted_stages = {
        "transformation",
        "canonical_adaptation",
        "quality",
        "normalization",
        "relationships",
        "reporting",
        "preflight",
    }
    stage_routes = {
        stage: route
        for stage in sorted(permitted_stages)
        if (route := _enum(routes.get(stage), _ROUTES)) is not None
    }
    if stage_routes:
        result["stage_routes"] = stage_routes
    result.update(sanitize_dataset_workload(raw))
    return result


def _resources(raw: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    _numbers(
        raw,
        result,
        {
            "cpu_seconds": 88_473_600.0,
            "parent_cpu_seconds": 88_473_600.0,
            "sample_interval_ms": 3_600_000.0,
            "sampling_duration_ms": 86_400_000.0,
        },
    )
    _integers(
        raw,
        result,
        {
            name: (0, _MAX_INTEGER)
            for name in (
                "sampled_peak_worker_rss_bytes",
                "sampled_peak_parent_rss_bytes",
                "sampled_peak_combined_rss_bytes",
                "sample_count",
                "sampling_failures",
            )
        },
    )
    if type(raw.get("sampling_complete")) is bool:
        result["sampling_complete"] = raw["sampling_complete"]
    return result


def _database(raw: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    _integers(
        raw,
        result,
        {
            name: (0, _MAX_INTEGER)
            for name in ("connection_count", "schema_check_count", "lock_retry_count")
        },
    )
    _numbers(
        raw,
        result,
        {name: 86_400_000.0 for name in ("connect_ms", "schema_ms", "lock_wait_ms")},
    )
    return result


def _integers(
    raw: Mapping[str, object],
    result: dict[str, object],
    fields: Mapping[str, tuple[int, int]],
) -> None:
    for name, (minimum, maximum) in fields.items():
        candidate = raw.get(name)
        if type(candidate) is int and minimum <= candidate <= maximum:
            result[name] = candidate


def _numbers(
    raw: Mapping[str, object], result: dict[str, object], fields: Mapping[str, float]
) -> None:
    for name, maximum in fields.items():
        candidate = raw.get(name)
        if (
            type(candidate) in {int, float}
            and 0 <= cast("int | float", candidate) <= maximum
        ):
            numeric = float(cast("int | float", candidate))
            if math.isfinite(numeric):
                result[name] = round(numeric, 6)


def _mapping(value: object) -> Mapping[str, object]:
    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}


def _enum(value: object, permitted: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in permitted else None


def _uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None
