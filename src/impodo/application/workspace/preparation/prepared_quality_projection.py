"""Named, bounded facts returned to the preparation quality consumer.

The projection describes index results, not a certification of an entire
prepared run. Adapters still own storage, lineage, and artifact checks. Fact
payloads retain their existing decoding and semantic validation in the quality
consumer. The same finding budget follows their transport and consumption.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from impodo.domain.preparation.quality_budget import QualityFindingBudget
from impodo.domain.preparation.staging_contracts import StagingDisposition


class PreparedQualityProjectionError(ValueError):
    """Reject malformed index results before they can become quality evidence."""


def _count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise PreparedQualityProjectionError(
            "Prepared data-check counts must be nonnegative integers."
        )
    return value


def _facts(value: object, width: int) -> tuple[Sequence[object], ...]:
    if not isinstance(value, (tuple, list)):
        raise PreparedQualityProjectionError("Prepared data-check facts are invalid.")
    for row in value:
        if not isinstance(row, (tuple, list)) or len(row) != width:
            raise PreparedQualityProjectionError(
                "Prepared data-check fact columns are invalid."
            )
    # Existing tuple pages and their payloads are reused without rebuilding rows.
    return tuple(value)


@dataclass(frozen=True, slots=True)
class PreparedQualityProjection:
    """Carry explicit accounting and finding facts for one indexed evaluation.

    Counts and membership are captured when the packet is constructed inside
    the adapter's existing local operation. Construction does not open a
    database, decode canonical rows, or authorize reuse in a later operation.
    """

    row_count: int
    physical_row_count: int
    direct_accounting: bool
    disposition_counts: Mapping[StagingDisposition, int]
    issue_rows: tuple[Sequence[object], ...] = ()
    collisions: tuple[Sequence[object], ...] = ()
    exception_rows: tuple[Sequence[object], ...] = ()
    excluded_row_ids: frozenset[str] = frozenset()
    precision_rows: tuple[Sequence[object], ...] = ()
    finding_budget: QualityFindingBudget = field(default_factory=QualityFindingBudget)

    def __post_init__(self) -> None:
        row_count = _count(self.row_count)
        physical_count = _count(self.physical_row_count)
        if type(self.direct_accounting) is not bool:
            raise PreparedQualityProjectionError(
                "Prepared source-accounting mode is invalid."
            )
        if self.direct_accounting and physical_count != row_count:
            raise PreparedQualityProjectionError(
                "Direct prepared rows do not match their physical row count."
            )
        if (
            not isinstance(self.disposition_counts, Mapping)
            or any(
                not isinstance(key, StagingDisposition)
                for key in self.disposition_counts
            )
            or set(self.disposition_counts) != set(StagingDisposition)
        ):
            raise PreparedQualityProjectionError(
                "Prepared row dispositions are incomplete."
            )
        counts = {
            disposition: _count(count)
            for disposition, count in self.disposition_counts.items()
        }
        if sum(counts.values()) != row_count:
            raise PreparedQualityProjectionError(
                "Prepared row dispositions do not match their row count."
            )
        if not isinstance(self.excluded_row_ids, (set, frozenset)) or any(
            not isinstance(row_id, str) for row_id in self.excluded_row_ids
        ):
            raise PreparedQualityProjectionError(
                "Prepared excluded-row membership is invalid."
            )
        if not isinstance(self.finding_budget, QualityFindingBudget):
            raise PreparedQualityProjectionError(
                "Prepared data-check finding budget is invalid."
            )
        object.__setattr__(self, "disposition_counts", MappingProxyType(counts))
        object.__setattr__(self, "excluded_row_ids", frozenset(self.excluded_row_ids))
        object.__setattr__(self, "issue_rows", _facts(self.issue_rows, 8))
        object.__setattr__(self, "collisions", _facts(self.collisions, 8))
        object.__setattr__(self, "exception_rows", _facts(self.exception_rows, 7))
        object.__setattr__(self, "precision_rows", _facts(self.precision_rows, 14))
