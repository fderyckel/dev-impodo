"""Fence a source gateway and its lazy page stream with frozen authority.

The supplied assertion checks one already bound SOURCE proof and releases its
local owners before returning. No local owner spans a delegate call or yield;
these wrappers do not bind, publish, retry, or mutate captured source data.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from impodo.application.odoo_source_capture_service import (
    OdooSourceCapturePort,
    OdooSourceCaptureSession,
)
from impodo.domain.odoo.contracts import MetadataSnapshot
from impodo.domain.odoo_provenance import OdooOriginBatch
from impodo.domain.odoo_source_capture import (
    CancellationProbe,
    OdooCaptureAccounting,
    OdooCapturePage,
    OdooCaptureSample,
    OdooSourceCaptureRequest,
)
from impodo.domain.shared.models import OdooReadIdentity, ProtectedOdooReadContext
from impodo.domain.workspace.errors import WorkspaceError

_SOURCE_BINDING = (
    "The reviewed source Odoo database binding could not be confirmed. Create "
    "a fresh source workspace for the declared current database before capturing again."
)


def _check(require_current: Callable[[], None]) -> None:
    try:
        require_current()
    except Exception as error:
        raise WorkspaceError(_SOURCE_BINDING) from error


def _read[T](require_current: Callable[[], None], call: Callable[[], T]) -> T:
    _check(require_current)
    result = call()
    _check(require_current)
    return result


@dataclass(frozen=True, slots=True)
class GenerationFencedOdooSourceCaptureSession:
    """Discard a page or accounting receipt when its source authority changed."""

    delegate: OdooSourceCaptureSession
    require_current: Callable[[], None]

    def pages(self) -> Iterable[OdooCapturePage]:
        pages = _read(self.require_current, lambda: iter(self.delegate.pages()))
        while True:
            _check(self.require_current)
            try:
                page = next(pages)
            except StopIteration:
                _check(self.require_current)
                return
            _check(self.require_current)
            yield page

    @property
    def matching_rows(self) -> int:
        return _read(self.require_current, lambda: self.delegate.matching_rows)

    @property
    def accounting(self) -> OdooCaptureAccounting:
        return _read(self.require_current, lambda: self.delegate.accounting)


@dataclass(frozen=True, slots=True)
class GenerationFencedOdooSourceCaptureGateway:
    """Keep the existing closed source API under one frozen SOURCE binding."""

    delegate: OdooSourceCapturePort
    require_current: Callable[[], None]

    def probe_identity(
        self,
        request: OdooSourceCaptureRequest,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> tuple[OdooReadIdentity, ProtectedOdooReadContext]:
        return _read(
            self.require_current,
            lambda: self.delegate.probe_identity(request, cancellation=cancellation),
        )

    def probe_schema(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> MetadataSnapshot:
        return _read(
            self.require_current,
            lambda: self.delegate.probe_schema(
                request, context, cancellation=cancellation
            ),
        )

    def open_capture(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        cancellation: CancellationProbe | None = None,
    ) -> OdooSourceCaptureSession:
        session = _read(
            self.require_current,
            lambda: self.delegate.open_capture(
                request, context, cancellation=cancellation
            ),
        )
        return GenerationFencedOdooSourceCaptureSession(session, self.require_current)

    def count_matching(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        limit: int,
        cancellation: CancellationProbe | None = None,
    ) -> int:
        return _read(
            self.require_current,
            lambda: self.delegate.count_matching(
                request, context, limit=limit, cancellation=cancellation
            ),
        )

    def scan_origins(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        cancellation: CancellationProbe | None = None,
        observe_page: Callable[[int, int], None] | None = None,
    ) -> tuple[OdooOriginBatch, ...]:
        def checked_page(completed: int, total: int) -> None:
            _check(self.require_current)
            if observe_page is not None:
                observe_page(completed, total)
            _check(self.require_current)

        return _read(
            self.require_current,
            lambda: self.delegate.scan_origins(
                request, context, cancellation=cancellation, observe_page=checked_page
            ),
        )

    def sample(
        self,
        request: OdooSourceCaptureRequest,
        context: ProtectedOdooReadContext,
        *,
        limit: int,
        cancellation: CancellationProbe | None = None,
    ) -> OdooCaptureSample:
        return _read(
            self.require_current,
            lambda: self.delegate.sample(
                request, context, limit=limit, cancellation=cancellation
            ),
        )
