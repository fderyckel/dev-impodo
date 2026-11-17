"""Publish remote admission waits without retaining local database owners."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_OBSERVER: ContextVar[Callable[[float | None], None] | None] = ContextVar(
    "remote_wait_observer", default=None
)


@contextmanager
def observe_remote_wait(observer: Callable[[float | None], None]) -> Iterator[None]:
    """Bind a worker-local progress and cancellation callback for remote waits."""
    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def report_remote_wait(seconds: float | None) -> bool:
    """Report remaining seconds, or resume with None; allow cancellation to propagate."""
    observer = _OBSERVER.get()
    if observer is None:
        return False
    observer(seconds)
    return True
