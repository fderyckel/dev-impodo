"""Validate shared-server writable roots without platform-specific I/O."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from impodo.web.composition.server_config import (
    SERVER_GID,
    SERVER_UID,
    ServerSettings,
)


class StorageMountKind(StrEnum):
    """Classify the lifecycle of a container-visible writable root."""

    PERSISTENT = "persistent"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StorageRootObservation:
    """Describe one root as observed by a deployment-specific probe."""

    path: PurePosixPath
    exists: bool
    is_directory: bool
    owner_uid: int | None
    owner_gid: int | None
    mode: int | None
    writable: bool
    mount_kind: StorageMountKind


@dataclass(frozen=True, slots=True)
class StorageValidationIssue:
    """Report one bounded mount-policy failure."""

    code: str
    path: PurePosixPath


class ServerStorageValidationError(RuntimeError):
    """Collect deterministic storage-policy failures for startup refusal."""

    def __init__(self, issues: tuple[StorageValidationIssue, ...]) -> None:
        self.issues = issues
        summary = ", ".join(f"{issue.path}: {issue.code}" for issue in issues)
        super().__init__(f"Server storage validation failed: {summary}")


class ServerStorageObservationError(RuntimeError):
    """Report that deployment storage facts could not be established."""


def validate_server_storage(
    settings: ServerSettings,
    observations: Iterable[StorageRootObservation],
) -> None:
    """Require safe writable roots and persistent customer artifact storage."""

    expected = {
        settings.artifact_root: StorageMountKind.PERSISTENT,
        settings.runtime_root: None,
        settings.temporary_root: None,
    }
    observed_by_path: dict[PurePosixPath, StorageRootObservation] = {}
    issues: list[StorageValidationIssue] = []
    for observation in observations:
        if observation.path not in expected:
            issues.append(StorageValidationIssue("unexpected_root", observation.path))
            continue
        if observation.path in observed_by_path:
            issues.append(
                StorageValidationIssue("duplicate_observation", observation.path)
            )
            continue
        observed_by_path[observation.path] = observation

    for path, required_mount_kind in expected.items():
        observation = observed_by_path.get(path)
        if observation is None:
            issues.append(StorageValidationIssue("not_observed", path))
            continue
        if not observation.exists:
            issues.append(StorageValidationIssue("missing", path))
            continue
        if not observation.is_directory:
            issues.append(StorageValidationIssue("not_directory", path))
        if observation.owner_uid != SERVER_UID:
            issues.append(StorageValidationIssue("wrong_owner_uid", path))
        if observation.owner_gid != SERVER_GID:
            issues.append(StorageValidationIssue("wrong_owner_gid", path))
        if observation.mode is None:
            issues.append(StorageValidationIssue("mode_unknown", path))
        elif observation.mode & 0o007:
            issues.append(StorageValidationIssue("world_accessible", path))
        if not observation.writable:
            issues.append(StorageValidationIssue("not_writable", path))
        if (
            required_mount_kind is not None
            and observation.mount_kind is not required_mount_kind
        ):
            issues.append(StorageValidationIssue("not_persistent", path))

    if issues:
        raise ServerStorageValidationError(tuple(issues))


__all__ = [
    "ServerStorageObservationError",
    "ServerStorageValidationError",
    "StorageMountKind",
    "StorageRootObservation",
    "StorageValidationIssue",
    "validate_server_storage",
]
