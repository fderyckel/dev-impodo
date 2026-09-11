"""Lifecycle-neutral FIFO execution for small in-process background jobs."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import logging
from threading import Condition, RLock, Thread


SerialJobTask = Callable[[], None]
SerialJobDiscard = Callable[[], None]


class SerialJobRunnerStoppedError(RuntimeError):
    """Raised when new work is submitted after shutdown begins."""


@dataclass(frozen=True, slots=True)
class _QueuedTask:
    task_id: str
    execute: SerialJobTask
    discard: SerialJobDiscard


class SerialJobRunner:
    """Run opaque tasks one at a time without owning their domain lifecycle."""

    def __init__(self, *, worker_name: str) -> None:
        if not worker_name.strip():
            raise ValueError("worker_name must not be blank")
        self._worker_name = worker_name.strip()[:100]
        self._lock = RLock()
        self._condition = Condition(self._lock)
        self._pending: deque[_QueuedTask] = deque()
        self._worker: Thread | None = None
        self._stopping = False

    def submit(
        self,
        task_id: str,
        execute: SerialJobTask,
        *,
        discard: SerialJobDiscard,
    ) -> None:
        """Queue an opaque task, starting the single daemon worker lazily."""

        if not task_id.strip():
            raise ValueError("task_id must not be blank")
        with self._condition:
            if self._stopping:
                raise SerialJobRunnerStoppedError(
                    "Background job runner is stopping"
                )
            self._pending.append(
                _QueuedTask(task_id.strip(), execute, discard)
            )
            if self._worker is None:
                self._worker = Thread(
                    target=self._run,
                    name=self._worker_name,
                    daemon=True,
                )
                self._worker.start()
            self._condition.notify()

    def shutdown(self) -> None:
        """Discard queued tasks without interrupting the task already running."""

        with self._condition:
            self._stopping = True
            discarded = tuple(self._pending)
            self._pending.clear()
            self._condition.notify_all()
        for task in discarded:
            self._discard(task)
        if self._worker is not None:
            self._worker.join(timeout=0.25)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._stopping:
                    self._condition.wait()
                if self._stopping:
                    return
                task = self._pending.popleft()
            try:
                task.execute()
            except Exception:
                logging.getLogger(__name__).exception(
                    "Unhandled serial background task failure",
                    extra={"task_id": task.task_id},
                )

    @staticmethod
    def _discard(task: _QueuedTask) -> None:
        try:
            task.discard()
        except Exception:
            logging.getLogger(__name__).exception(
                "Could not discard queued background task",
                extra={"task_id": task.task_id},
            )


__all__ = [
    "SerialJobRunner",
    "SerialJobRunnerStoppedError",
]
