"""Coordinate fail-closed startup and controlled server draining."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import NoReturn, Protocol

from impodo.web.composition.server_active_work import (
    ServerActiveWorkLease,
    ServerActiveWorkOwner,
    ServerSafeCheckpointError,
    ServerWorkAdmissionClosedError,
)
from impodo.web.composition.server_config import ServerSettings
from impodo.web.composition.server_lifecycle import (
    ReadinessCheck,
    ServerLifecycle,
    ServerLifecycleError,
    ServerLifecycleOwner,
    ServerPhase,
)
from impodo.web.composition.server_storage import (
    ServerStorageObservationError,
    ServerStorageValidationError,
    StorageRootObservation,
    validate_server_storage,
)


class ServerStorageObserver(Protocol):
    """Observe the configured roots without deciding storage policy."""

    def __call__(
        self,
        settings: ServerSettings,
    ) -> tuple[StorageRootObservation, ...]: ...


class ServerPostgreSQLInspector(Protocol):
    """Prove PostgreSQL reachability and schema in one bounded operation."""

    def __call__(self, settings: ServerSettings) -> None: ...


class ServerDependencyProbeError(RuntimeError):
    """Report bounded dependency unavailability from a server adapter."""


class ServerPostgreSQLProbeError(ServerDependencyProbeError):
    """Report that PostgreSQL could not complete its reachability proof."""


class ServerSchemaProbeError(ServerDependencyProbeError):
    """Report that the PostgreSQL control schema is incompatible."""


@dataclass(frozen=True, slots=True)
class ServerRuntimePorts:
    """Supply deployment-owned startup probes."""

    storage_observer: ServerStorageObserver
    postgresql_inspector: ServerPostgreSQLInspector


class ServerStartupStage(StrEnum):
    """Name an internal startup stage without containing dependency detail."""

    STORAGE = "storage"
    POSTGRESQL = "postgresql"
    SCHEMA = "schema"


class ServerStartupError(RuntimeError):
    """Report one failed startup stage without exposing its original message."""

    def __init__(self, stage: ServerStartupStage) -> None:
        self.stage = stage
        super().__init__(f"Server startup check failed: {stage.value}")


class ServerDrainError(RuntimeError):
    """Report that admitted work has not reached a safe checkpoint."""


class ServerRuntimeCoordinator:
    """Own one process startup order, readiness state, and safe drain."""

    def __init__(
        self,
        settings: ServerSettings,
        ports: ServerRuntimePorts,
        *,
        lifecycle_owner: ServerLifecycleOwner | None = None,
        active_work_owner: ServerActiveWorkOwner | None = None,
    ) -> None:
        self._settings = settings
        self._ports = ports
        self._lifecycle_owner = lifecycle_owner or ServerLifecycleOwner()
        self._active_work_owner = active_work_owner or ServerActiveWorkOwner()
        self._operation_lock = Lock()

    def snapshot(self) -> ServerLifecycle:
        """Return the immutable lifecycle snapshot used by health routes."""

        return self._lifecycle_owner.snapshot()

    def _fail_startup(
        self,
        stage: ServerStartupStage,
        error: Exception,
    ) -> NoReturn:
        if self._lifecycle_owner.snapshot().phase is ServerPhase.STARTING:
            self._lifecycle_owner.mark_stopped()
        raise ServerStartupError(stage) from error

    def startup(self) -> ServerLifecycle:
        """Run every startup proof in order and admit work only once."""

        with self._operation_lock:
            initial = self._lifecycle_owner.snapshot()
            if initial.phase is not ServerPhase.STARTING or initial.satisfied_checks:
                raise ServerLifecycleError(
                    "Server startup requires a fresh starting lifecycle"
                )

            self._lifecycle_owner.record_check(
                ReadinessCheck.CONFIGURATION,
                satisfied=True,
            )
            try:
                observations = self._ports.storage_observer(self._settings)
                validate_server_storage(self._settings, observations)
            except (
                ServerStorageObservationError,
                ServerStorageValidationError,
            ) as error:
                self._fail_startup(ServerStartupStage.STORAGE, error)
            self._lifecycle_owner.record_check(
                ReadinessCheck.ARTIFACT_STORAGE,
                satisfied=True,
            )
            self._lifecycle_owner.record_check(
                ReadinessCheck.RUNTIME_STORAGE,
                satisfied=True,
            )

            try:
                self._ports.postgresql_inspector(self._settings)
            except ServerPostgreSQLProbeError as error:
                self._fail_startup(ServerStartupStage.POSTGRESQL, error)
            except ServerSchemaProbeError as error:
                self._lifecycle_owner.record_check(
                    ReadinessCheck.POSTGRESQL,
                    satisfied=True,
                )
                self._fail_startup(ServerStartupStage.SCHEMA, error)
            self._lifecycle_owner.record_check(
                ReadinessCheck.POSTGRESQL,
                satisfied=True,
            )
            self._lifecycle_owner.record_check(
                ReadinessCheck.SCHEMA,
                satisfied=True,
            )
            self._active_work_owner.open_admission()
            return self._lifecycle_owner.begin_accepting()

    def begin_work(self) -> ServerActiveWorkLease:
        """Admit one operation atomically with the public lifecycle state."""

        with self._operation_lock:
            if not self._lifecycle_owner.snapshot().is_ready:
                raise ServerWorkAdmissionClosedError("Server work admission is closed")
            return self._active_work_owner.begin_work()

    def begin_draining(self) -> ServerLifecycle:
        """Withdraw readiness before any safe-checkpoint wait begins."""

        with self._operation_lock:
            lifecycle = self._lifecycle_owner.snapshot()
            if lifecycle.phase is ServerPhase.ACCEPTING:
                lifecycle = self._lifecycle_owner.begin_draining()
                self._active_work_owner.close_admission(
                    timeout_seconds=self._settings.drain_timeout_seconds
                )
                return lifecycle
            if lifecycle.phase is ServerPhase.STARTING:
                self._active_work_owner.close_admission(
                    timeout_seconds=self._settings.drain_timeout_seconds
                )
                return self._lifecycle_owner.mark_stopped()
            if lifecycle.phase is ServerPhase.DRAINING:
                self._active_work_owner.close_admission(
                    timeout_seconds=self._settings.drain_timeout_seconds
                )
            return lifecycle

    def complete_draining(self) -> ServerLifecycle:
        """Wait for durable checkpoints, then mark the process stopped."""

        with self._operation_lock:
            lifecycle = self._lifecycle_owner.snapshot()
            if lifecycle.phase is ServerPhase.STOPPED:
                return lifecycle
            if lifecycle.phase is ServerPhase.STARTING:
                return self._lifecycle_owner.mark_stopped()
            if lifecycle.phase is ServerPhase.ACCEPTING:
                raise ServerLifecycleError(
                    "Server readiness must be withdrawn before drain completion"
                )
            try:
                self._active_work_owner.wait_for_safe_checkpoint()
            except ServerSafeCheckpointError as error:
                raise ServerDrainError(
                    "Server drain did not reach a safe checkpoint"
                ) from error
            return self._lifecycle_owner.mark_stopped()

    def drain(self) -> ServerLifecycle:
        """Withdraw readiness, wait for safe checkpoints, and stop."""

        self.begin_draining()
        return self.complete_draining()


__all__ = [
    "ServerDependencyProbeError",
    "ServerDrainError",
    "ServerPostgreSQLInspector",
    "ServerPostgreSQLProbeError",
    "ServerRuntimeCoordinator",
    "ServerRuntimePorts",
    "ServerSafeCheckpointError",
    "ServerSchemaProbeError",
    "ServerStartupError",
    "ServerStartupStage",
    "ServerStorageObserver",
    "ServerWorkAdmissionClosedError",
]
