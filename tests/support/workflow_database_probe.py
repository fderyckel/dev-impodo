"""Count physical opens and checked schema generations inside one measured operation."""

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event
from unittest.mock import patch

from impodo.adapters.duckdb.schema import forward_upgrades
from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory


@dataclass
class StoreWork:
    opens: Counter[str] = field(default_factory=Counter)
    schemas: Counter[str] = field(default_factory=Counter)
    first_open: Event | None = None


_observed = ContextVar("workflow_store_work", default=None)


@contextmanager
def observe_store_work(first_open=None):
    work = StoreWork(first_open=first_open)
    token = _observed.set(work)
    try:
        yield work
    finally:
        _observed.reset(token)


@contextmanager
def instrument_stores():
    connect = DuckDbConnectionFactory._connect_with_lock_wait
    check = forward_upgrades._ensure_current_schema

    def open_store(factory, path, config):
        work = _observed.get()
        if work is not None and work.first_open is not None:
            work.first_open.set()
        result = connect(factory, path, config)
        if work is not None:
            work.opens[path.name] += 1
        return result

    def check_store(connection, **kwargs):
        work = _observed.get()
        if work is not None:
            work.schemas[kwargs["expected_generation"]] += 1
        return check(connection, **kwargs)

    with (
        patch.object(DuckDbConnectionFactory, "_connect_with_lock_wait", open_store),
        patch.object(forward_upgrades, "_ensure_current_schema", check_store),
    ):
        yield
