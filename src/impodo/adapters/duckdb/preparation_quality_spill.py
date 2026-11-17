"""Keep evaluation collections in a private disposable SQLite scratch file.

SQLite supplies indexed updates and canonical-key ordering with a bounded page
cache. It holds no source database owner and accepts no pre-existing file or
saved format. The workflow closes this synchronous store before returning;
DuckDB publication remains the sole durable quality-evidence transaction.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator, MutableMapping, Sequence
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from typing import overload

from impodo.application.workspace.preparation.quality_finding_store import (
    FindingSequence,
    QualityFindingStore,
)
from impodo.domain.preparation.quality_budget import QUALITY_FINDING_INDEX_MAX_BYTES
from impodo.domain.workspace.errors import WorkspaceError


class _SpillValues[T](Sequence[T]):
    def __init__(
        self, connection: sqlite3.Connection, name: str, decode: Callable[[str], T]
    ) -> None:
        self.connection, self.name, self.decode = connection, name, decode

    def __len__(self) -> int:
        return int(
            self.connection.execute(
                "SELECT count(*) FROM entry WHERE collection = ?", (self.name,)
            ).fetchone()[0]
        )

    @overload
    def __getitem__(self, index: int) -> T: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[T]: ...

    def __getitem__(self, index: int | slice) -> T | Sequence[T]:
        if isinstance(index, slice):
            start, stop, step = index.indices(len(self))
            if step != 1:
                return tuple(self[item] for item in range(start, stop, step))
            return tuple(
                self.decode(row[0])
                for row in self.connection.execute(
                    "SELECT value FROM entry WHERE collection = ? ORDER BY key LIMIT ? OFFSET ?",
                    (self.name, max(0, stop - start), start),
                )
            )
        normalized = index + len(self) if index < 0 else index
        if normalized < 0:
            raise IndexError(index)
        row = self.connection.execute(
            "SELECT value FROM entry WHERE collection = ? ORDER BY key LIMIT 1 OFFSET ?",
            (self.name, normalized),
        ).fetchone()
        if row is None:
            raise IndexError(index)
        return self.decode(row[0])

    def __iter__(self) -> Iterator[T]:
        for batch in self.iter_batches(128):
            yield from batch

    def iter_batches(self, batch_size: int) -> Iterator[Sequence[T]]:
        """Keep decoded publication pages within two MiB or one large value."""
        cursor = self.connection.execute(
            "SELECT value FROM entry WHERE collection = ? ORDER BY key", (self.name,)
        )
        try:
            batch: list[T] = []
            byte_count = 0
            while row := cursor.fetchone():
                size = len(row[0].encode("utf-8"))
                if batch and (
                    len(batch) >= batch_size or byte_count + size > 2 * 1024 * 1024
                ):
                    yield tuple(batch)
                    batch.clear()
                    byte_count = 0
                batch.append(self.decode(row[0]))
                byte_count += size
            if batch:
                yield tuple(batch)
        finally:
            cursor.close()


class _SpillMap[K, V](MutableMapping[K, V]):
    def __init__(
        self,
        connection: sqlite3.Connection,
        name: str,
        encode_key: Callable[[K], str],
        decode_key: Callable[[str], K],
        encode_value: Callable[[V], str],
        decode_value: Callable[[str], V],
    ) -> None:
        self.connection, self.name = connection, name
        self.encode_key, self.decode_key = encode_key, decode_key
        self.encode_value, self.decode_value = encode_value, decode_value

    def __getitem__(self, key: K) -> V:
        row = self.connection.execute(
            "SELECT value FROM entry WHERE collection = ? AND key = ?",
            (self.name, self.encode_key(key)),
        ).fetchone()
        if row is None:
            raise KeyError(key)
        return self.decode_value(row[0])

    def __setitem__(self, key: K, value: V) -> None:
        encoded = self.encode_value(value)
        if len(encoded.encode("utf-8")) > QUALITY_FINDING_INDEX_MAX_BYTES:
            raise WorkspaceError(
                "One data-check record exceeds the working memory budget. "
                "No partial review evidence was published."
            )
        self.connection.execute(
            "INSERT OR REPLACE INTO entry VALUES (?, ?, ?)",
            (self.name, self.encode_key(key), encoded),
        )

    def __delitem__(self, key: K) -> None:
        cursor = self.connection.execute(
            "DELETE FROM entry WHERE collection = ? AND key = ?",
            (self.name, self.encode_key(key)),
        )
        if not cursor.rowcount:
            raise KeyError(key)

    def __len__(self) -> int:
        return len(_SpillValues(self.connection, self.name, self.decode_value))

    def __iter__(self) -> Iterator[K]:
        cursor = self.connection.execute(
            "SELECT key FROM entry WHERE collection = ? ORDER BY key", (self.name,)
        )
        try:
            while batch := cursor.fetchmany(128):
                for row in batch:
                    yield self.decode_key(row[0])
        finally:
            cursor.close()


class _SpillSequence[T](_SpillValues[T], FindingSequence[T]):
    def __init__(
        self,
        connection: sqlite3.Connection,
        name: str,
        encode: Callable[[T], str],
        decode: Callable[[str], T],
    ) -> None:
        super().__init__(connection, name, decode)
        self.encode = encode
        self._count = super().__len__()

    def __len__(self) -> int:
        return self._count

    def append(self, value: T) -> None:
        self.connection.execute(
            "INSERT INTO entry VALUES (?, ?, ?)",
            (self.name, f"{self._count:020d}", self.encode(value)),
        )
        self._count += 1


class _QualitySpill:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def mapping[K, V](
        self,
        name: str,
        *,
        encode_key: Callable[[K], str],
        decode_key: Callable[[str], K],
        encode_value: Callable[[V], str],
        decode_value: Callable[[str], V],
    ) -> MutableMapping[K, V]:
        return _SpillMap(
            self.connection, name, encode_key, decode_key, encode_value, decode_value
        )

    def values[V](self, name: str, *, decode_value: Callable[[str], V]) -> Sequence[V]:
        return _SpillValues(self.connection, name, decode_value)

    def sequence[T](
        self,
        name: str,
        *,
        encode: Callable[[T], str],
        decode: Callable[[str], T],
    ) -> FindingSequence[T]:
        return _SpillSequence(self.connection, name, encode, decode)


@contextmanager
def quality_finding_store() -> Iterator[QualityFindingStore]:
    """Own one private scratch file, including cleanup after interrupted work."""

    with TemporaryDirectory(prefix="impodo-quality-") as directory:
        connection = sqlite3.connect(f"{directory}/findings.sqlite")
        try:
            connection.execute("PRAGMA cache_size = -2048")
            connection.execute("PRAGMA temp_store = FILE")
            connection.execute("PRAGMA mmap_size = 0")
            # This file is disposable; durable receipts are published in DuckDB.
            connection.execute("PRAGMA journal_mode = OFF")
            connection.execute(
                "CREATE TABLE entry (collection TEXT, key TEXT, value TEXT, "
                "PRIMARY KEY (collection, key)) WITHOUT ROWID"
            )
            connection.execute("BEGIN")
            yield _QualitySpill(connection)
        finally:
            connection.close()
