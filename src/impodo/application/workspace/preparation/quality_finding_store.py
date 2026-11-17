"""Temporary, bounded storage ports for a complete quality evaluation.

The caller owns one store through quality publication and normalization, then
closes it on success or failure. Only canonical published evidence is durable.
Codecs keep application objects out of concrete storage implementations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping, Sequence
from contextlib import AbstractContextManager
from typing import Protocol, overload, runtime_checkable


class FindingSequence[T](Sequence[T]):
    """Replay and append findings without retaining preceding pages."""

    def append(self, value: T) -> None:
        """Append one encoded finding to this evaluation's storage."""
        raise NotImplementedError


@runtime_checkable
class FindingBatchReader[T](Protocol):
    """Replay rows with both row and encoded-byte working limits."""

    def iter_batches(self, batch_size: int) -> Iterator[Sequence[T]]:
        """Yield every value once in canonical order, without oversized pages."""
        ...


class QualityFindingStore(Protocol):
    """Supply disk-backed collections within one synchronous evaluation."""

    def mapping[K, V](
        self,
        name: str,
        *,
        encode_key: Callable[[K], str],
        decode_key: Callable[[str], K],
        encode_value: Callable[[V], str],
        decode_value: Callable[[str], V],
    ) -> MutableMapping[K, V]:
        """Create a named mapping with explicit application-owned codecs."""
        ...

    def values[V](
        self,
        name: str,
        *,
        decode_value: Callable[[str], V],
    ) -> Sequence[V]:
        """Replay mapping values in encoded-key order, including bounded slices."""
        ...

    def sequence[T](
        self,
        name: str,
        *,
        encode: Callable[[T], str],
        decode: Callable[[str], T],
    ) -> FindingSequence[T]:
        """Create an ordered append-only sequence with bounded replay."""
        ...


@runtime_checkable
class QualityFindingStoreFactory(Protocol):
    """Create scratch storage without opening the prepared evidence database."""

    def quality_finding_store(self) -> AbstractContextManager[QualityFindingStore]:
        """Release every temporary resource when the owning workflow ends."""
        ...


class FindingSet[T](Sequence[T]):
    """Store membership as mapping keys and expose ordered, bounded slices."""

    def __init__(self, values: MutableMapping[T, T], ordered: Sequence[T]) -> None:
        self._values = values
        self._ordered = ordered

    def add(self, value: T) -> None:
        """Retain one membership decision, deduplicating repeated findings."""
        self._values[value] = value

    def __contains__(self, value: object) -> bool:
        return value in self._values

    def __len__(self) -> int:
        return len(self._values)

    @overload
    def __getitem__(self, index: int) -> T: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[T]: ...

    def __getitem__(self, index: int | slice) -> T | Sequence[T]:
        return self._ordered[index]

    def __iter__(self) -> Iterator[T]:
        return iter(self._ordered)
