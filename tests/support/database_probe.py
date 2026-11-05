"""Small test-only probes for persistence I/O regression assertions."""

from __future__ import annotations

from typing import Any
from contextlib import contextmanager
from unittest.mock import patch

from impodo.adapters.duckdb.unit_of_work import DuckDbConnectionFactory, _read_databases


@contextmanager
def count_duckdb_statements():
    """Count SQL without breaking the retained owner's schema-validation key.

    A proxy with an unregistered identity disables scoped validation and can
    make the probe itself introduce repeated schema checks. Keep both cursor
    identities bound to the same owner for the proxy's exact lifetime.
    """

    statements = []
    original = DuckDbConnectionFactory.connect

    @contextmanager
    def connect(factory, *args, **kwargs):
        with original(factory, *args, **kwargs) as connection:
            proxy = StatementCountingConnection(connection, statements)
            keys = getattr(_read_databases, "cursor_database_keys", None)
            if keys is not None and id(connection) in keys:
                keys[id(proxy)] = keys[id(connection)]
            try:
                yield proxy
            finally:
                if keys is not None:
                    keys.pop(id(proxy), None)

    with patch.object(DuckDbConnectionFactory, "connect", connect):
        yield statements


class StatementCountingConnection:
    """Proxy one database context while recording executed SQL statements."""

    def __init__(self, connection: Any, statements: list[str]) -> None:
        self._connection = connection
        self._active = connection
        self._statements = statements

    def __enter__(self) -> "StatementCountingConnection":
        self._active = self._connection.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        return self._connection.__exit__(*args)

    def execute(self, statement: str, *args: object, **kwargs: object) -> Any:
        self._statements.append(statement)
        return self._active.execute(statement, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._active, name)
