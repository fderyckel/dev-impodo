"""Initialize and verify bounded native threads in a preparation child only.

Call ``configure_preparation_runtime`` before importing preparation adapters.
The helper never runs at module import and never changes the parent process.
``IMPODO_PREPARATION_THREADS`` opts this worker into one, two, or four Polars
threads; an existing generic Polars override remains authoritative when that
preparation-specific setting is absent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum

from .columnar_runtime import POLARS_DEFAULT_MAX_THREADS

PREPARATION_THREADS_ENV = "IMPODO_PREPARATION_THREADS"
POLARS_THREADS_ENV = "POLARS_MAX_THREADS"
PREPARATION_THREAD_CHOICES = frozenset({1, 2, 4})


class PreparationThreadSource(StrEnum):
    """Identify the selected configuration without recording environment text."""

    DEFAULT = "default"
    PREPARATION_OVERRIDE = "preparation_override"
    INHERITED_POLARS = "inherited_polars"


class PreparationRuntimeConfigurationError(ValueError):
    """Stop preparation when the requested native configuration is unproven."""

    def __init__(self, message: str, *, failure_code: str) -> None:
        super().__init__(message)
        self.failure_code = failure_code


@dataclass(frozen=True, slots=True)
class PreparationRuntimeConfiguration:
    """Carry requested and observed counts from this exact child process."""

    requested_threads: int
    effective_threads: int
    source: PreparationThreadSource

    def diagnostic_payload(self) -> dict[str, int | str]:
        """Return allowlisted technical configuration without raw variables."""

        return {
            "requested_polars_threads": self.requested_threads,
            "effective_polars_threads": self.effective_threads,
            "thread_configuration_source": self.source.value,
        }


def configure_preparation_runtime() -> PreparationRuntimeConfiguration:
    """Apply the child-only request and verify the native pool before work.

    Importing this module is inert. Its caller must run this function only in
    the spawned preparation child. A previously initialized pool is accepted
    only when its actual count matches the request; environment assignment
    alone never establishes that the native configuration took effect.
    """

    requested, source = _thread_request()
    os.environ[POLARS_THREADS_ENV] = str(requested)

    # Import after applying the child setting; Polars initializes its native
    # pool lazily, so an earlier import is safe only if the verified pool agrees.
    import polars

    effective = polars.thread_pool_size()
    if effective != requested:
        raise PreparationRuntimeConfigurationError(
            "The preparation thread setting could not be applied before "
            "native processing initialized. Restart Impodo before trying again.",
            failure_code="PREPARATION_THREAD_CONFIGURATION_MISMATCH",
        )
    return PreparationRuntimeConfiguration(requested, effective, source)


def _thread_request() -> tuple[int, PreparationThreadSource]:
    preparation_value = os.environ.get(PREPARATION_THREADS_ENV)
    if preparation_value is not None:
        if preparation_value not in {"1", "2", "4"}:
            raise PreparationRuntimeConfigurationError(
                "The preparation thread setting must be 1, 2, or 4.",
                failure_code="PREPARATION_THREAD_CONFIGURATION_INVALID",
            )
        return int(preparation_value), PreparationThreadSource.PREPARATION_OVERRIDE

    polars_value = os.environ.get(POLARS_THREADS_ENV)
    if polars_value is not None:
        try:
            requested = int(polars_value)
        except ValueError:
            requested = 0
        if requested <= 0:
            raise PreparationRuntimeConfigurationError(
                "The existing Polars thread setting must be a positive integer.",
                failure_code="PREPARATION_THREAD_CONFIGURATION_INVALID",
            )
        return requested, PreparationThreadSource.INHERITED_POLARS
    return POLARS_DEFAULT_MAX_THREADS, PreparationThreadSource.DEFAULT
