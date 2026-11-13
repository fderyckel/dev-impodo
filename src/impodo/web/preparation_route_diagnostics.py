"""Retain bounded dataset route facts without customer table or field names."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import cast

from impodo.application.workspace.preparation.fallback_performance import (
    FALLBACK_REASON_CODES,
    MAX_DATASET_ROUTE_OBSERVATIONS,
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
        "UNOBSERVED",
    }
)
_ADAPTATIONS = frozenset(
    {
        "UNOBSERVED",
        "NATIVE_PROJECTION",
        "PYTHON_ROW_ADAPTATION",
        "PYTHON_RECORDS",
        "MATERIALIZED_RECORDS",
    }
)
_OUTCOMES = frozenset(
    {
        "PREDICTED",
        "STARTED",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "INTERRUPTED",
    }
)
_COVERAGE_COUNTS = frozenset(
    {
        "dataset_count",
        "captured_dataset_count",
        "omitted_dataset_count",
        "row_count",
        "captured_row_count",
        "omitted_row_count",
    }
)
_MAX_ROUTE_BYTES = 24 * 1024


def sanitize_dataset_workload(value: Mapping[str, object]) -> dict[str, object]:
    """Recheck route snapshots and keep repeated dataset observations non-additive."""

    items = value.get("dataset_routes")
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    used_bytes = 2
    for item in (
        items[:MAX_DATASET_ROUTE_OBSERVATIONS] if isinstance(items, list) else ()
    ):
        raw = _mapping(item)
        identity = raw.get("dataset_hash")
        rows = raw.get("row_count")
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", identity)
            or identity in seen
            or type(rows) is not int
            or not 0 <= rows <= 2**53 - 1
        ):
            continue
        result: dict[str, object] = {"dataset_hash": identity, "row_count": rows}
        for field, allowed in (
            ("predicted_route", _ROUTES),
            ("actual_route", _ROUTES),
            ("adaptation_route", _ADAPTATIONS),
            ("outcome", _OUTCOMES),
        ):
            candidate = raw.get(field)
            if isinstance(candidate, str) and candidate in allowed:
                result[field] = candidate
        codes = raw.get("reason_codes")
        result["reason_codes"] = (
            sorted(
                {
                    code
                    for code in codes[:64]
                    if isinstance(code, str) and code in FALLBACK_REASON_CODES
                }
            )[:8]
            if isinstance(codes, list)
            else []
        )
        omitted = raw.get("omitted_reason_count")
        if type(omitted) is int and 0 <= omitted <= 1_000_000:
            result["omitted_reason_count"] = omitted
        duration = raw.get("duration_ms")
        if (
            isinstance(duration, (int, float))
            and not isinstance(duration, bool)
            and 0 <= duration <= 86_400_000
            and math.isfinite(duration)
        ):
            result["duration_ms"] = round(float(duration), 3)
        size = (
            len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode())
            + 1
        )
        if used_bytes + size > _MAX_ROUTE_BYTES:
            break
        records.append(result)
        seen.add(identity)
        used_bytes += size
    result_workload: dict[str, object] = {}
    if isinstance(items, list):
        result_workload["dataset_routes"] = records
    raw_coverage = _mapping(value.get("dataset_route_coverage"))
    coverage = {
        name: item
        for name in sorted(_COVERAGE_COUNTS)
        if type(item := raw_coverage.get(name)) is int and 0 <= item <= 2**53 - 1
    }
    if isinstance(items, list):
        captured_rows = sum(cast("int", item["row_count"]) for item in records)
        coverage["captured_dataset_count"] = len(records)
        coverage["captured_row_count"] = captured_rows
        datasets = coverage.get("dataset_count")
        if datasets is not None:
            if datasets >= len(records):
                coverage["omitted_dataset_count"] = datasets - len(records)
            else:
                coverage.pop("dataset_count", None)
                coverage.pop("omitted_dataset_count", None)
        rows = coverage.get("row_count")
        if rows is not None:
            if rows >= captured_rows:
                coverage["omitted_row_count"] = rows - captured_rows
            else:
                coverage.pop("row_count", None)
                coverage.pop("omitted_row_count", None)
    if coverage:
        result_workload["dataset_route_coverage"] = coverage
    return result_workload


def _mapping(value: object) -> Mapping[str, object]:
    """Narrow untrusted optional diagnostic content without inventing facts."""

    return cast("Mapping[str, object]", value) if isinstance(value, Mapping) else {}
