"""Small test-only probes for persistence I/O regression assertions."""

from __future__ import annotations

from typing import Any


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
