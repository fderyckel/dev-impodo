"""Define privacy-safe shared-server lifecycle and readiness state."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from threading import Lock


class ServerLifecycleError(RuntimeError):
    """Report an invalid shared-server lifecycle transition."""


class ServerPhase(StrEnum):
    """Describe whether the process may accept new work."""

    STARTING = "starting"
    ACCEPTING = "accepting"
    DRAINING = "draining"
    STOPPED = "stopped"


class ReadinessCheck(StrEnum):
    """Name internal conditions without exposing their detail publicly."""

    CONFIGURATION = "configuration"
    ARTIFACT_STORAGE = "artifact_storage"
    RUNTIME_STORAGE = "runtime_storage"
    POSTGRESQL = "postgresql"
    SCHEMA = "schema"


_ALL_CHECKS = frozenset(ReadinessCheck)


@dataclass(frozen=True, slots=True)
class ServerLifecycle:
    """Track readiness and controlled drain as an immutable state machine."""

    phase: ServerPhase = ServerPhase.STARTING
    satisfied_checks: frozenset[ReadinessCheck] = frozenset()

    @property
    def blockers(self) -> tuple[ReadinessCheck, ...]:
        """Return deterministic internal blockers for operators and tests."""

        return tuple(sorted(_ALL_CHECKS - self.satisfied_checks, key=str))

    @property
    def is_ready(self) -> bool:
        """Return whether the process may receive new work."""

        return self.phase is ServerPhase.ACCEPTING and not self.blockers

    def with_check(
        self,
        check: ReadinessCheck,
        *,
        satisfied: bool,
    ) -> ServerLifecycle:
        """Record one internal health condition before or during service."""

        if self.phase in (ServerPhase.DRAINING, ServerPhase.STOPPED):
            raise ServerLifecycleError(
                f"Readiness checks cannot change while {self.phase.value}"
            )
        checks = set(self.satisfied_checks)
        if satisfied:
            checks.add(check)
        else:
            checks.discard(check)
        phase = self.phase
        if phase is ServerPhase.ACCEPTING and len(checks) != len(_ALL_CHECKS):
            phase = ServerPhase.STARTING
        return replace(self, phase=phase, satisfied_checks=frozenset(checks))

    def begin_accepting(self) -> ServerLifecycle:
        """Enter service only after every internal readiness check passes."""

        if self.phase is not ServerPhase.STARTING:
            raise ServerLifecycleError(
                f"Cannot begin accepting while {self.phase.value}"
            )
        if self.blockers:
            raise ServerLifecycleError(
                "Cannot begin accepting while readiness checks are blocked"
            )
        return replace(self, phase=ServerPhase.ACCEPTING)

    def begin_draining(self) -> ServerLifecycle:
        """Stop accepting new work before controlled process shutdown."""

        if self.phase is not ServerPhase.ACCEPTING:
            raise ServerLifecycleError(
                f"Cannot begin draining while {self.phase.value}"
            )
        return replace(self, phase=ServerPhase.DRAINING)

    def mark_stopped(self) -> ServerLifecycle:
        """Complete a failed startup or an explicit drain."""

        if self.phase not in (ServerPhase.STARTING, ServerPhase.DRAINING):
            raise ServerLifecycleError(f"Cannot stop directly while {self.phase.value}")
        return replace(self, phase=ServerPhase.STOPPED)

    def readiness_payload(self) -> dict[str, str]:
        """Return a public status that contains no tenancy or dependency detail."""

        return {"status": "ready" if self.is_ready else "not_ready"}


class ServerLifecycleOwner:
    """Own one process lifecycle and serialize its immutable transitions."""

    def __init__(self, initial: ServerLifecycle | None = None) -> None:
        self._lock = Lock()
        self._lifecycle = initial or ServerLifecycle()

    def snapshot(self) -> ServerLifecycle:
        """Return the current immutable process lifecycle."""

        with self._lock:
            return self._lifecycle

    def record_check(
        self,
        check: ReadinessCheck,
        *,
        satisfied: bool,
    ) -> ServerLifecycle:
        """Record one readiness check and return the resulting snapshot."""

        with self._lock:
            self._lifecycle = self._lifecycle.with_check(
                check,
                satisfied=satisfied,
            )
            return self._lifecycle

    def begin_accepting(self) -> ServerLifecycle:
        """Admit new work after every required check passes."""

        with self._lock:
            self._lifecycle = self._lifecycle.begin_accepting()
            return self._lifecycle

    def begin_draining(self) -> ServerLifecycle:
        """Withdraw readiness before waiting for safe checkpoints."""

        with self._lock:
            self._lifecycle = self._lifecycle.begin_draining()
            return self._lifecycle

    def mark_stopped(self) -> ServerLifecycle:
        """Complete a failed startup or controlled drain."""

        with self._lock:
            self._lifecycle = self._lifecycle.mark_stopped()
            return self._lifecycle


def liveness_payload() -> dict[str, str]:
    """Return a dependency-free response when the process loop can answer."""

    return {"status": "alive"}


__all__ = [
    "ReadinessCheck",
    "ServerLifecycle",
    "ServerLifecycleError",
    "ServerLifecycleOwner",
    "ServerPhase",
    "liveness_payload",
]
