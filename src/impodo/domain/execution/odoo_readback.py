"""Application-facing contract for bounded post-write Odoo reads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence


MAX_READBACK_IDS = 50
MAX_READBACK_LOOKUPS = 20


class OdooReadbackError(RuntimeError):
    """A protected read-back could not produce trustworthy records."""


@dataclass(frozen=True, slots=True)
class ReadbackRecord:
    odoo_id: int
    values: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ExternalIdBinding:
    external_id: str
    model: str
    odoo_id: int


@dataclass(frozen=True, slots=True)
class ReadbackLookup:
    """One exact reviewed business-key lookup in a bounded batch."""

    domain: tuple[tuple[str, str, Any], ...]
    fields: tuple[str, ...] = ()


class OdooReadbackReader(Protocol):
    @property
    def target_hash(self) -> str: ...

    @property
    def scope_hash(self) -> str: ...

    @property
    def imports_external_ids(self) -> bool: ...

    def read_ids(
        self,
        model: str,
        identifiers: Sequence[int],
        fields: Sequence[str],
    ) -> tuple[ReadbackRecord, ...]: ...

    def find_records(
        self,
        model: str,
        domain: Sequence[tuple[str, str, Any]],
        fields: Sequence[str],
    ) -> tuple[ReadbackRecord, ...]: ...

    def find_records_many(
        self,
        model: str,
        lookups: Sequence[ReadbackLookup],
    ) -> tuple[tuple[ReadbackRecord, ...], ...]: ...

    def read_external_ids(
        self,
        external_ids: Sequence[str],
    ) -> tuple[ExternalIdBinding, ...]: ...
