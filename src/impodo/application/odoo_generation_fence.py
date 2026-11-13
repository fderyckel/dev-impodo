"""Fence reviewed Odoo transport calls with a frozen database binding.

Composition supplies an assertion that checks the exact binding captured for
the authorized workspace. It must complete and release its local resources
before returning; it must never bind a workspace to the latest generation.
These wrappers own no database, journal, retry, or binding mutation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from impodo.domain.execution.odoo_readback import (
    ExternalIdBinding,
    OdooReadbackError,
    OdooReadbackReader,
    ReadbackLookup,
    ReadbackRecord,
)
from impodo.domain.execution.odoo_write import (
    OdooWriteExecutor,
    OdooWriteOutcomeUnknown,
    OdooWriteRejected,
)

_BEFORE_CALL = (
    "The reviewed Odoo database binding could not be confirmed. Prepare a fresh "
    "restore or workspace for the declared current destination before continuing."
)
_AFTER_WRITE = (
    "The Odoo database binding could not be confirmed after the write. "
    "Its outcome is uncertain; do not retry this write."
)
_READBACK = (
    "The reviewed Odoo database binding could not be confirmed. Read-back "
    "results cannot verify this run; confirm the current destination first."
)


@dataclass(frozen=True, slots=True)
class GenerationFencedOdooWriteExecutor:
    """Preserve native transport outcomes while refusing a stale generation."""

    delegate: OdooWriteExecutor
    require_current: Callable[[], None]

    @property
    def target_hash(self) -> str:
        return self.delegate.target_hash

    @property
    def scope_hash(self) -> str:
        return self.delegate.scope_hash

    def _check_before_call(self) -> None:
        try:
            self.require_current()
        except Exception as error:
            raise OdooWriteRejected(_BEFORE_CALL) from error

    def _read[T](self, call: Callable[[], T]) -> T:
        self._check_before_call()
        result = call()
        self._check_before_call()
        return result

    def _write[T](self, call: Callable[[], T]) -> T:
        self._check_before_call()
        result = call()
        try:
            self.require_current()
        except Exception as error:
            # The delegate has already returned a receipt. A generation change
            # must not turn it into either a committed result or a safe retry.
            raise OdooWriteOutcomeUnknown(_AFTER_WRITE) from error
        return result

    def find_ids(
        self, model: str, domain: Sequence[tuple[str, str, Any]]
    ) -> tuple[int, ...]:
        return self._read(lambda: self.delegate.find_ids(model, domain))

    def find_ids_many(
        self, model: str, domains: Sequence[Sequence[tuple[str, str, Any]]]
    ) -> tuple[tuple[int, ...], ...]:
        return self._read(lambda: self.delegate.find_ids_many(model, domains))

    def create_rows(
        self, model: str, values: Sequence[Mapping[str, Any]]
    ) -> tuple[int, ...]:
        return self._write(lambda: self.delegate.create_rows(model, values))

    def load_create_rows(
        self,
        model: str,
        values: Sequence[Mapping[str, Any]],
        external_ids: Sequence[str],
    ) -> tuple[int, ...]:
        return self._write(
            lambda: self.delegate.load_create_rows(model, values, external_ids)
        )

    def update_row(self, model: str, record_id: int, values: Mapping[str, Any]) -> None:
        self._write(lambda: self.delegate.update_row(model, record_id, values))

    def update_rows(
        self, model: str, record_ids: Sequence[int], values: Mapping[str, Any]
    ) -> None:
        self._write(lambda: self.delegate.update_rows(model, record_ids, values))

    def read_projected_ids(
        self,
        model: str,
        identifiers: Sequence[int],
        projection_field: str,
        target_model: str,
    ) -> tuple[int, ...]:
        return self._read(
            lambda: self.delegate.read_projected_ids(
                model, identifiers, projection_field, target_model
            )
        )


@dataclass(frozen=True, slots=True)
class GenerationFencedOdooReadbackReader:
    """Exclude records from a changed database from journal verification."""

    delegate: OdooReadbackReader
    require_current: Callable[[], None]

    @property
    def target_hash(self) -> str:
        return self.delegate.target_hash

    @property
    def scope_hash(self) -> str:
        return self.delegate.scope_hash

    @property
    def imports_external_ids(self) -> bool:
        return self.delegate.imports_external_ids

    def _check(self) -> None:
        try:
            self.require_current()
        except Exception as error:
            raise OdooReadbackError(_READBACK) from error

    def _read[T](self, call: Callable[[], T]) -> T:
        self._check()
        result = call()
        self._check()
        return result

    def read_ids(
        self, model: str, identifiers: Sequence[int], fields: Sequence[str]
    ) -> tuple[ReadbackRecord, ...]:
        return self._read(lambda: self.delegate.read_ids(model, identifiers, fields))

    def find_records(
        self,
        model: str,
        domain: Sequence[tuple[str, str, Any]],
        fields: Sequence[str],
    ) -> tuple[ReadbackRecord, ...]:
        return self._read(lambda: self.delegate.find_records(model, domain, fields))

    def find_records_many(
        self, model: str, lookups: Sequence[ReadbackLookup]
    ) -> tuple[tuple[ReadbackRecord, ...], ...]:
        return self._read(lambda: self.delegate.find_records_many(model, lookups))

    def read_external_ids(
        self, external_ids: Sequence[str]
    ) -> tuple[ExternalIdBinding, ...]:
        return self._read(lambda: self.delegate.read_external_ids(external_ids))
