"""Run a local page read with database handles bounded to its worker call."""

from typing import Callable, ParamSpec, TypeVar

from starlette.concurrency import run_in_threadpool

from impodo.adapters.duckdb.unit_of_work import (
    retain_databases_for_operation,
    retain_databases_for_read,
)


P = ParamSpec("P")
T = TypeVar("T")


async def run_page_read(
    function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Release retained database handles before returning to the event loop."""

    def read() -> T:
        with retain_databases_for_read():
            return function(*args, **kwargs)

    return await run_in_threadpool(read)


async def run_local_operation(
    function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run one local command off-loop while reusing its database owners.

    The callable must not await, start background work, or contact a remote
    service. Repository transactions remain independent and every retained
    database owner is released before this function returns.
    """

    def operate() -> T:
        with retain_databases_for_operation():
            return function(*args, **kwargs)

    return await run_in_threadpool(operate)
