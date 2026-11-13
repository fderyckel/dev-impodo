"""Observe bounded dataset routes without changing evaluation or evidence.

Predictions come from the existing capability manifest. Execution observations
are reported only at actual evaluator/adaptation boundaries. Metadata is bounded
and value-free; an unsupported evaluator does not imply whole-run materialization.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from time import perf_counter
from typing import TYPE_CHECKING

from impodo.domain.compiler.columnar_transformation import COLUMNAR_CAPABILITY_MATRIX
from impodo.domain.shared.models import canonical_json_bytes

if TYPE_CHECKING:
    from impodo.domain.workspace.contracts import SourceSelection

    from .preparation_capability import PreparationCapabilityManifest

MAX_DATASET_ROUTE_OBSERVATIONS = 128
FALLBACK_REASON_CODES = frozenset(
    {
        "ADVANCED_QUALITY_RULES_MATERIALIZE",
        "BOUNDED_PRODUCER_GRAPH",
        "BOUNDED_RELATIONSHIP_INDEX",
        "COLUMNAR_MAPPING_UNSUPPORTED",
        "DERIVED_OR_NON_DIRECT_DATASET",
        "FROZEN_SNAPSHOT_STREAM",
        "GROUPED_EXACT_JOIN_STREAM",
        "LOOKUP_ONLY_DERIVED_DATASET",
        "LOOKUP_VALUE_STREAM",
        "NON_DIRECT_QUALITY_INPUT",
        "NOT_EXECUTED_DURING_PREPARATION",
        "PARQUET_ROW_INCLUSION",
        "PREPARED_SNAPSHOT_VALUE_PROJECTION",
        "REFERENCE_RESOLUTION_MATERIALIZES",
        "RELATIONAL_IDENTITY_SCALE_UNQUALIFIED",
        "ROW_JSON_COMPATIBILITY_PATH",
        "RUNTIME_DATA_SHAPE_GUARD",
        "SET_BASED_RELATIONSHIP_ROUTE_UNAVAILABLE",
        "SOURCE_SNAPSHOT_UNAVAILABLE",
    }
    | {
        item.fallback_code
        for item in COLUMNAR_CAPABILITY_MATRIX
        if item.fallback_code is not None
    }
)


@dataclass(slots=True)
class _DatasetObservation:
    dataset_hash: str
    row_count: int
    predicted_route: str
    reason_codes: tuple[str, ...]
    actual_route: str = "UNOBSERVED"
    adaptation_route: str = "UNOBSERVED"
    outcome: str = "PREDICTED"
    started: float | None = None
    duration_ms: float | None = None

    def payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "dataset_hash": self.dataset_hash,
            "row_count": self.row_count,
            "predicted_route": self.predicted_route,
            "actual_route": self.actual_route,
            "adaptation_route": self.adaptation_route,
            "reason_codes": list(self.reason_codes),
            "outcome": self.outcome,
        }
        if self.duration_ms is not None:
            result["duration_ms"] = self.duration_ms
        return result


class DatasetPreparationPerformance:
    """Keep one metadata record per captured dataset, regardless of rule count.

    ``duration_ms`` measures the dataset's evaluator and canonical adaptation
    together; it excludes later run-wide publication, quality and normalization.
    An interrupted dataset has no authority over the enclosing job's outcome.
    """

    def __init__(
        self,
        capability: PreparationCapabilityManifest,
        selection: SourceSelection,
        *,
        mapping_hash: str,
        source_selection_hash: str,
        sink: Callable[[dict[str, object]], None],
        clock: Callable[[], float] = perf_counter,
    ) -> None:
        self._sink = sink
        self._clock = clock
        self._datasets: dict[str, _DatasetObservation] = {}
        routes = {item.dataset_id: item for item in capability.datasets}
        transformation = next(
            (item for item in capability.stages if item.stage == "transformation"),
            None,
        )
        row_count = 0
        for dataset in sorted(selection.datasets, key=lambda item: item.dataset_id):
            row_count += dataset.row_count
            if len(self._datasets) >= MAX_DATASET_ROUTE_OBSERVATIONS:
                continue
            route = routes.get(dataset.dataset_id)
            predicted = route.behavior if route is not None else (
                transformation.behavior if transformation is not None else None
            )
            reasons = route.reason_codes if route is not None else (
                transformation.reason_codes if transformation is not None else ()
            )
            digest = sha256(canonical_json_bytes({
                "dataset_id": dataset.dataset_id,
                "mapping_hash": mapping_hash,
                "source_selection_hash": source_selection_hash,
            })).hexdigest()
            self._datasets[dataset.dataset_id] = _DatasetObservation(
                dataset_hash="sha256:" + digest,
                row_count=dataset.row_count,
                predicted_route=predicted.value if predicted is not None else "UNOBSERVED",
                reason_codes=tuple(sorted(set(reasons) & FALLBACK_REASON_CODES)),
            )
        captured_rows = sum(item.row_count for item in self._datasets.values())
        self._coverage = {
            "dataset_count": len(selection.datasets),
            "captured_dataset_count": len(self._datasets),
            "omitted_dataset_count": len(selection.datasets) - len(self._datasets),
            "row_count": row_count,
            "captured_row_count": captured_rows,
            "omitted_row_count": row_count - captured_rows,
        }

    def publish(self) -> None:
        """Replace the bounded snapshot; successive observations are not additive."""
        try:
            self._sink({"workload": {
                "dataset_routes": [item.payload() for item in self._datasets.values()],
                "dataset_route_coverage": dict(self._coverage),
            }})
        except Exception:  # noqa: BLE001 - optional diagnostics cannot change preparation
            return

    def start(self, dataset_id: str, route: str) -> None:
        """Observe one selected evaluator immediately before its dataset work."""
        try:
            item = self._datasets.get(dataset_id)
            if item is None or route not in {"NATIVE_COLUMNAR", "BOUNDED_PYTHON"}:
                return
            item.actual_route = route
            item.outcome = "STARTED"
            item.started = self._clock()
            item.duration_ms = None
            self.publish()
        except Exception:  # noqa: BLE001 - optional diagnostics cannot change preparation
            return

    def adaptation(self, dataset_id: str, route: str) -> None:
        """Observe the actual canonical adapter without emitting a per-row event."""
        item = self._datasets.get(dataset_id)
        if item is not None and route in {
            "NATIVE_PROJECTION", "PYTHON_ROW_ADAPTATION", "PYTHON_RECORDS",
        }:
            item.adaptation_route = route

    def finish(self, dataset_id: str) -> None:
        """Mark one fully adapted dataset, excluding subsequent run-wide work."""
        try:
            item = self._datasets.get(dataset_id)
            if item is None or item.started is None or item.outcome != "STARTED":
                return
            item.duration_ms = round(max(0.0, (self._clock() - item.started) * 1000), 3)
            item.outcome = "SUCCEEDED"
            self.publish()
        except Exception:  # noqa: BLE001 - optional diagnostics cannot change preparation
            return

    def interrupt_active(self) -> None:
        """Retain partial work; the supervisor alone knows failure or cancellation."""
        try:
            for item in self._datasets.values():
                if item.outcome == "STARTED" and item.started is not None:
                    item.duration_ms = round(max(0.0, (self._clock() - item.started) * 1000), 3)
                    item.outcome = "INTERRUPTED"
            self.publish()
        except Exception:  # noqa: BLE001 - optional diagnostics cannot change preparation
            return
