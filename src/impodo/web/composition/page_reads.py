"""Run bounded local database work away from the web event loop."""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from contextvars import ContextVar
from inspect import isawaitable, iscoroutine
from typing import ParamSpec, TypeVar

from starlette.concurrency import run_in_threadpool

from impodo.adapters.duckdb.request_timing import (
    collect_duckdb_request_timings,
    current_duckdb_request_timings,
)
from impodo.adapters.duckdb.unit_of_work import (
    retain_databases_for_operation,
    retain_databases_for_read,
)
from impodo.application.shared.performance import current_performance

P = ParamSpec("P")
T = TypeVar("T")
DatabaseScope = Callable[[], AbstractContextManager[None]]
_OBSERVING_PERFORMANCE_DB: ContextVar[bool] = ContextVar(
    "impodo_performance_database_scope", default=False,
)


def _run_in_database_scope_sync[**P, T](
    database_scope: DatabaseScope,
    function: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run one synchronous callable inside one bounded database scope."""

    with database_scope():
        result = function(*args, **kwargs)
        if isawaitable(result):
            if iscoroutine(result):
                result.close()
            raise TypeError("Local database work must use a synchronous callable")
        return result


async def _run_in_database_scope[**P, T](
    database_scope: DatabaseScope,
    function: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Keep the database scope and synchronous callable on one worker."""

    return await run_in_threadpool(
        _run_in_database_scope_sync,
        database_scope,
        function,
        *args,
        **kwargs,
    )


async def run_page_read[**P, T](
    function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run one local page read off-loop while reusing its database owners.

    The callable must not await, start background work, or contact a remote
    service. Every retained database owner is released before this function
    returns.
    """

    return await _run_in_database_scope(
        retain_databases_for_read,
        function,
        *args,
        **kwargs,
    )


async def run_local_operation[**P, T](
    function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run one local command off-loop while reusing its database owners.

    The callable must not await, start background work, or contact a remote
    service. Repository transactions remain independent and every retained
    database owner is released before this function returns.
    """

    return await _run_in_database_scope(
        retain_databases_for_operation,
        function,
        *args,
        **kwargs,
    )


def run_local_operation_sync[**P, T](
    function: Callable[P, T], *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run one local command on the current worker in a bounded scope.

    Background workers use this form because they are already off the web event
    loop. The scope is still closed before the worker performs remote I/O or
    reports completion.
    """

    performance = current_performance()
    if performance is None or _OBSERVING_PERFORMANCE_DB.get():
        return _run_in_database_scope_sync(
            retain_databases_for_operation, function, *args, **kwargs,
        )
    existing = current_duckdb_request_timings()
    scope = nullcontext(existing) if existing is not None else collect_duckdb_request_timings()
    with scope as timings:
        before = (
            timings.connection_count, timings.schema_check_count,
            timings.lock_retry_count, timings.connect_ms,
            timings.schema_ms, timings.lock_wait_ms,
        )
        token = _OBSERVING_PERFORMANCE_DB.set(True)
        try:
            return _run_in_database_scope_sync(
                retain_databases_for_operation, function, *args, **kwargs,
            )
        finally:
            _OBSERVING_PERFORMANCE_DB.reset(token)
            performance.metrics({
                "database_connection_count": timings.connection_count - before[0],
                "database_schema_check_count": timings.schema_check_count - before[1],
                "database_lock_retry_count": timings.lock_retry_count - before[2],
                "database_connection_ms": timings.connect_ms - before[3],
                "database_schema_ms": timings.schema_ms - before[4],
                "database_lock_wait_ms": timings.lock_wait_ms - before[5],
            })
