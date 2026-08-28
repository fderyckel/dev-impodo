"""Application-facing contract for reviewed Odoo writes."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence


MAX_IDENTITY_LOOKUP_KEYS = 100


class OdooWriteError(RuntimeError):
    """Base class for safe Stage-J writer failures."""


class OdooWriteRejected(OdooWriteError):
    """Odoo definitively rejected a request, so it did not commit."""


class OdooWriteOutcomeUnknown(OdooWriteError):
    """The connection failed after send and commit cannot be inferred."""


class OdooWriteExecutor(Protocol):
    """Small application-facing surface implemented by the native adapter."""

    @property
    def target_hash(self) -> str: ...

    @property
    def scope_hash(self) -> str: ...

    def find_ids(
        self,
        model: str,
        domain: Sequence[tuple[str, str, Any]],
    ) -> tuple[int, ...]: ...

    def find_ids_many(
        self,
        model: str,
        domains: Sequence[Sequence[tuple[str, str, Any]]],
    ) -> tuple[tuple[int, ...], ...]: ...

    def create_rows(
        self,
        model: str,
        values: Sequence[Mapping[str, Any]],
    ) -> tuple[int, ...]: ...

    def load_create_rows(
        self,
        model: str,
        values: Sequence[Mapping[str, Any]],
        external_ids: Sequence[str],
    ) -> tuple[int, ...]: ...

    def update_row(
        self,
        model: str,
        record_id: int,
        values: Mapping[str, Any],
    ) -> None: ...
