"""Consumer-owned read capabilities for bounded prepared quality evidence.

Indexed facts, relationship findings, and encoded publication batches are
independent capabilities. An indexed reader need not provide a relationship
graph or encoded rows. Consumers still validate returned facts and evidence
bindings; satisfying a protocol does not prove that stored evidence is valid.
These calls run inside the caller's existing synchronous local operation and
must not retain a database owner beyond that operation.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Protocol, runtime_checkable

from impodo.application.workspace.preparation.prepared_quality_projection import (
    PreparedQualityProjection,
)
from impodo.domain.target_numeric_precision import PreparedNumericField

from .quality_finding_store import QualityFindingStore


@runtime_checkable
class EncodedPreparedRowReader(Protocol):
    """Read canonical publication batches using the publisher's local context."""

    def iter_encoded_batches(
        self,
        connection: object,
        batch_size: int,
    ) -> Iterator[Sequence[Sequence[object]]]:
        """Yield bounded metadata and exact canonical JSON in persisted order."""
        ...


@runtime_checkable
class PreparedQualityIndexReader(Protocol):
    """Supply validated quality facts, source accounting, and row membership."""

    def bounded_quality_index(
        self,
        physical_rows: Mapping[str, tuple[int, ...]],
        *,
        numeric_fields: tuple[PreparedNumericField, ...] = (),
        retained_excluded_coordinates: frozenset[tuple[str, int]] = frozenset(),
        finding_store: QualityFindingStore | None = None,
    ) -> PreparedQualityProjection | None:
        """Return validated narrow facts, or decline this indexed route."""
        ...

    def iter_quality_index_batches(
        self,
        connection: object,
        batch_size: int,
    ) -> Iterator[Sequence[Sequence[object]]]:
        """Yield ordered row decisions without reconstructing complete values."""
        ...

    def iter_accounting_index_batches(
        self,
        connection: object,
        batch_size: int,
    ) -> Iterator[Sequence[Sequence[object]]]:
        """Yield physical coordinates and their ordered canonical row links."""
        ...

    def contains_row_id(self, row_id: str) -> bool:
        """Check membership in the same bound canonical evidence."""
        ...


@runtime_checkable
class PreparedRelationshipFindingReader(Protocol):
    """Evaluate ordinary incoming relationships independently of identity groups."""

    def bounded_relationship_findings(
        self,
        unsafe_row_ids: Sequence[str],
        propagating_datasets: Sequence[str],
        *, finding_store: QualityFindingStore | None = None,
    ) -> Iterable[Sequence[object]]:
        """Yield bounded readiness findings under the current quality policies."""
        ...


@runtime_checkable
class PreparedIdentityGroupFindingReader(Protocol):
    """Evaluate the complete group when incoming parents supply record identity."""

    def bounded_identity_group_findings(
        self,
        unsafe_row_ids: Sequence[str],
        propagating_datasets: Sequence[str],
    ) -> Iterable[Sequence[object]]:
        """Yield forward and reverse findings after bounded graph convergence."""
        ...
