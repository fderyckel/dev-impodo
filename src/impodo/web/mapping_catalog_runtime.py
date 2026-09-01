"""Bound mapping-catalogue projection work and obsolete browser searches."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Lock
from typing import Generic, TypeVar


ResultT = TypeVar("ResultT")


class MappingCatalogCapacityError(RuntimeError):
    """Raised when every bounded catalogue scheduler slot is active."""


class MappingCatalogProjectionCache(Generic[ResultT]):
    """Share bounded immutable projections identified by saved evidence."""

    def __init__(self, *, maximum_entries: int = 64) -> None:
        if maximum_entries < 1:
            raise ValueError("maximum_entries must be positive")
        self.maximum_entries = maximum_entries
        self._lock = Lock()
        self._entries: OrderedDict[Hashable, ResultT] = OrderedDict()
        self._inflight: dict[Hashable, Future[ResultT]] = {}

    def get_or_create(
        self,
        key: Hashable,
        factory: Callable[[], ResultT],
    ) -> tuple[ResultT, bool]:
        """Return one projection and whether existing work supplied it."""

        with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                return cached, True
            pending = self._inflight.get(key)
            if pending is None:
                pending = Future()
                self._inflight[key] = pending
                builds_projection = True
            else:
                builds_projection = False

        if not builds_projection:
            return pending.result(), True

        try:
            created = factory()
        except BaseException as error:
            with self._lock:
                self._inflight.pop(key, None)
                pending.set_exception(error)
            raise

        with self._lock:
            self._inflight.pop(key, None)
            self._entries[key] = created
            self._entries.move_to_end(key)
            while len(self._entries) > self.maximum_entries:
                self._entries.popitem(last=False)
            pending.set_result(created)
        return created, False

    @property
    def entry_count(self) -> int:
        """Return the bounded number of completed cached projections."""

        with self._lock:
            return len(self._entries)


@dataclass(slots=True)
class _EditorSearchState:
    lock: asyncio.Lock
    latest_generation: int


class MappingCatalogSearchCoordinator:
    """Coalesce each editor and serialize projection work per workspace."""

    def __init__(self, *, maximum_editors: int = 256) -> None:
        if maximum_editors < 1:
            raise ValueError("maximum_editors must be positive")
        self.maximum_editors = maximum_editors
        self._states: OrderedDict[Hashable, _EditorSearchState] = OrderedDict()
        self._work_locks: OrderedDict[Hashable, asyncio.Lock] = OrderedDict()

    async def run_latest(
        self,
        key: Hashable,
        generation: int,
        operation: Callable[[], Awaitable[ResultT]],
        *,
        work_key: Hashable | None = None,
    ) -> ResultT | None:
        """Run ``operation`` only while ``generation`` remains authoritative."""

        if generation < 1:
            raise ValueError("generation must be positive")
        state = self._state_for(key, generation)
        if generation < state.latest_generation:
            return None
        state.latest_generation = generation

        async with state.lock:
            if generation != state.latest_generation:
                return None
            work_lock = self._work_lock_for(work_key if work_key is not None else key)
            async with work_lock:
                if generation != state.latest_generation:
                    return None
                result = await operation()
            if generation != state.latest_generation:
                return None
            return result

    def _state_for(
        self,
        key: Hashable,
        generation: int,
    ) -> _EditorSearchState:
        state = self._states.get(key)
        if state is not None:
            self._states.move_to_end(key)
            return state
        self._discard_idle_editors()
        if len(self._states) >= self.maximum_editors:
            raise MappingCatalogCapacityError(
                "Every mapping catalogue editor slot is active"
            )
        state = _EditorSearchState(
            lock=asyncio.Lock(),
            latest_generation=generation,
        )
        self._states[key] = state
        return state

    def _discard_idle_editors(self) -> None:
        if len(self._states) < self.maximum_editors:
            return
        for key, state in tuple(self._states.items()):
            if not state.lock.locked():
                del self._states[key]
                if len(self._states) < self.maximum_editors:
                    return
    def _work_lock_for(self, key: Hashable) -> asyncio.Lock:
        lock = self._work_locks.get(key)
        if lock is not None:
            self._work_locks.move_to_end(key)
            return lock
        if len(self._work_locks) >= self.maximum_editors:
            for candidate_key, candidate in tuple(self._work_locks.items()):
                if not candidate.locked():
                    del self._work_locks[candidate_key]
                    break
            if len(self._work_locks) >= self.maximum_editors:
                raise MappingCatalogCapacityError(
                    "Every mapping catalogue workspace slot is active"
                )
        lock = asyncio.Lock()
        self._work_locks[key] = lock
        return lock

    @property
    def editor_count(self) -> int:
        """Return the currently tracked bounded editor count."""

        return len(self._states)
