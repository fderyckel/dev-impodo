"""Let a deployment bound local storage phases without coupling use cases to it."""

from collections.abc import Callable
from typing import Protocol


class LocalOperation(Protocol):
    """Run one synchronous local phase and release its resources before returning."""

    def __call__[T](self, function: Callable[[], T], /) -> T: ...


def direct_local_operation[T](function: Callable[[], T]) -> T:
    """Preserve direct execution for callers without deployment-owned resources."""

    return function()
