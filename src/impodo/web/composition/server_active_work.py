"""Own process-local work admission and its safe shutdown checkpoint."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition, Lock
from time import monotonic


class ServerWorkAdmissionClosedError(RuntimeError):
    """Report that the process may no longer admit application work."""


class ServerSafeCheckpointError(RuntimeError):
    """Report that admitted work did not reach its process-safe checkpoint."""


@dataclass(frozen=True, slots=True)
class ServerActiveWork:
    """Describe process-local admission without identifying customer work."""

    accepting: bool
    active_count: int


class ServerActiveWorkLease:
    """Keep one admitted operation active until its ASGI work is complete."""

    def __init__(self, owner: ServerActiveWorkOwner, lease_id: int) -> None:
        self._owner = owner
        self._lease_id = lease_id

    def complete(self) -> None:
        """Release this operation exactly once at a safe process checkpoint."""

        self._owner._complete(self._lease_id)


class ServerActiveWorkOwner:
    """Serialize one process's admission, active work, and drain deadline."""

    def __init__(
        self,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        lock = Lock()
        self._condition = Condition(lock)
        self._monotonic_clock = monotonic_clock
        self._accepting = False
        self._opened = False
        self._drain_deadline: float | None = None
        self._next_lease_id = 1
        self._active_lease_ids: set[int] = set()

    def snapshot(self) -> ServerActiveWork:
        """Return privacy-safe process counts for coordination and tests."""

        with self._condition:
            return ServerActiveWork(
                accepting=self._accepting,
                active_count=len(self._active_lease_ids),
            )

    def open_admission(self) -> ServerActiveWork:
        """Open work admission once after every startup proof has passed."""

        with self._condition:
            if self._opened:
                raise ServerWorkAdmissionClosedError(
                    "Server work admission cannot be reopened"
                )
            self._opened = True
            self._accepting = True
            return self._snapshot_unlocked()

    def begin_work(self) -> ServerActiveWorkLease:
        """Admit one operation or fail before any application work begins."""

        with self._condition:
            if not self._accepting:
                raise ServerWorkAdmissionClosedError("Server work admission is closed")
            lease_id = self._next_lease_id
            self._next_lease_id += 1
            self._active_lease_ids.add(lease_id)
            return ServerActiveWorkLease(self, lease_id)

    def close_admission(self, *, timeout_seconds: int) -> ServerActiveWork:
        """Close admission and start the one process-wide drain deadline."""

        with self._condition:
            self._accepting = False
            if self._drain_deadline is None:
                self._drain_deadline = self._monotonic_clock() + timeout_seconds
            return self._snapshot_unlocked()

    def wait_for_safe_checkpoint(self) -> None:
        """Wait only for time left on the deadline established at drain start."""

        with self._condition:
            if self._accepting or self._drain_deadline is None:
                raise ServerSafeCheckpointError(
                    "Server work admission must close before checkpoint wait"
                )
            remaining = max(
                0.0,
                self._drain_deadline - self._monotonic_clock(),
            )
            reached = self._condition.wait_for(
                lambda: not self._active_lease_ids,
                timeout=remaining,
            )
            if not reached:
                raise ServerSafeCheckpointError(
                    "Active server work did not reach a safe checkpoint"
                )

    def _complete(self, lease_id: int) -> None:
        with self._condition:
            if lease_id not in self._active_lease_ids:
                raise ServerSafeCheckpointError(
                    "Server work lease was already completed"
                )
            self._active_lease_ids.remove(lease_id)
            if not self._active_lease_ids:
                self._condition.notify_all()

    def _snapshot_unlocked(self) -> ServerActiveWork:
        """Return state while the caller owns the condition lock."""

        return ServerActiveWork(
            accepting=self._accepting,
            active_count=len(self._active_lease_ids),
        )


__all__ = [
    "ServerActiveWork",
    "ServerActiveWorkLease",
    "ServerActiveWorkOwner",
    "ServerSafeCheckpointError",
    "ServerWorkAdmissionClosedError",
]
