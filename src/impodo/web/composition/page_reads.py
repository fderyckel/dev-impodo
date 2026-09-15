"""Run a local page read with database handles bounded to its worker call."""

from typing import Callable, ParamSpec, TypeVar

from starlette.concurrency import run_in_threadpool

from impodo.adapters.duckdb.unit_of_work import retain_databases_for_read


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
