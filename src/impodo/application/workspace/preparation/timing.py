"""Non-invasive timing helpers for preparation application stages."""

from __future__ import annotations

from contextlib import contextmanager
from time import perf_counter
from typing import Callable, Iterator


PreparationTimingReporter = Callable[[str, float, str], None]


@contextmanager
def timed_preparation_stage(
    reporter: PreparationTimingReporter | None,
    stage: str,
) -> Iterator[None]:
    """Report one stage without allowing diagnostics to change its outcome."""

    started = perf_counter()
    try:
        yield
    except BaseException:
        _report_safely(
            reporter,
            stage,
            (perf_counter() - started) * 1000,
            "failed",
        )
        raise
    else:
        _report_safely(
            reporter,
            stage,
            (perf_counter() - started) * 1000,
            "completed",
        )


def _report_safely(
    reporter: PreparationTimingReporter | None,
    stage: str,
    duration_ms: float,
    outcome: str,
) -> None:
    if reporter is None:
        return
    try:
        reporter(stage, duration_ms, outcome)
    except Exception:
        # Optional support evidence must never alter governed preparation.
        return
