"""Shared contracts for the clean Migration Project persistence foundation.

This module contains validation and failure types used by the Project,
DataVersion, MigrationRun, and MigrationWorkspace roots introduced from Phase
M1 onward. It has no database or web dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Callable, Mapping
from uuid import UUID

from .access import ActorIdentity


_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")


class MigrationFoundationError(ValueError):
    """Base error for the clean Migration Project foundation."""


class MigrationNotFoundError(MigrationFoundationError):
    """Raised when an exact aggregate identity does not exist."""


class MigrationConflictError(MigrationFoundationError):
    """Raised when optimistic state changed before a command committed."""


class MigrationOperationReplayError(MigrationConflictError):
    """Raised when one request identity is replayed with different meaning."""


class MigrationIdentifierConfusionError(MigrationFoundationError):
    """Raised when an identifier belongs to a different aggregate namespace."""


class MigrationStorageCompatibilityError(MigrationFoundationError):
    """Reject a database from another storage generation without mutating it."""

    def __init__(self, database_path: str, reset_command: str) -> None:
        super().__init__(
            "Impodo found development storage from another data contract at "
            f"{database_path}. It was not changed. Review the reset plan with "
            f"`{reset_command}`."
        )
        self.database_path = database_path
        self.reset_command = reset_command


class MigrationOperationKind(StrEnum):
    PROJECT_CREATE = "PROJECT_CREATE"
    DATA_VERSION_CREATE = "DATA_VERSION_CREATE"
    DATA_VERSION_FREEZE = "DATA_VERSION_FREEZE"
    MIGRATION_RUN_CREATE = "MIGRATION_RUN_CREATE"
    MIGRATION_WORKSPACE_CREATE = "MIGRATION_WORKSPACE_CREATE"
    WORKSPACE_SOURCE_PROJECT = "WORKSPACE_SOURCE_PROJECT"
    RECIPE_PUBLISH = "RECIPE_PUBLISH"
    MIGRATION_RUN_PLAN = "MIGRATION_RUN_PLAN"
    CUTOVER_PLAN_REVISION = "CUTOVER_PLAN_REVISION"
    CUTOVER_PLAN_QUALIFICATION = "CUTOVER_PLAN_QUALIFICATION"
    PROJECT_CUTOVER_SELECTION = "PROJECT_CUTOVER_SELECTION"
    PRODUCTION_RUN_SETUP = "PRODUCTION_RUN_SETUP"
    PRODUCTION_RUN_ACTIVATE = "PRODUCTION_RUN_ACTIVATE"


class MigrationOperationState(StrEnum):
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"


FaultInjector = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class MigrationOperationIntent:
    """Record one restart-safe operation owned by its target aggregate."""

    operation_id: str
    project_id: str
    owner_kind: str
    owner_id: str
    kind: MigrationOperationKind
    request_hash: str
    expected_revision: int | None
    state: MigrationOperationState
    stage: str
    detail: Mapping[str, object]
    last_error: str
    actor: ActorIdentity
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        for value, name in (
            (self.operation_id, "operation_id"),
            (self.project_id, "project_id"),
            (self.owner_id, "owner_id"),
        ):
            require_uuid(value, name)
        require_hash(self.request_hash, "request_hash")
        object.__setattr__(self, "kind", MigrationOperationKind(self.kind))
        object.__setattr__(self, "state", MigrationOperationState(self.state))
        if self.expected_revision is not None:
            require_revision(
                self.expected_revision,
                "expected_revision",
            )
        required_text(self.owner_kind, "owner_kind", maximum=80)
        required_text(self.stage, "stage", maximum=80)
        require_aware(self.created_at, "created_at")
        require_aware(self.updated_at, "updated_at")


def require_uuid(value: str, name: str) -> str:
    """Return one lower-case canonical UUID or reject the identifier."""

    try:
        canonical = str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise MigrationFoundationError(f"{name} is invalid") from error
    if canonical != value:
        raise MigrationFoundationError(f"{name} is invalid")
    return value


def require_hash(value: str, name: str) -> str:
    """Return one canonical SHA-256 content identifier."""

    if _HASH.fullmatch(value) is None:
        raise MigrationFoundationError(f"{name} is invalid")
    return value


def require_revision(value: int, name: str = "revision") -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise MigrationFoundationError(f"{name} is invalid")
    return value


def required_text(value: str, name: str, *, maximum: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise MigrationFoundationError(f"{name} must not be blank")
    if len(cleaned) > maximum:
        raise MigrationFoundationError(f"{name} is too long")
    return cleaned


def optional_text(value: str, name: str, *, maximum: int) -> str:
    cleaned = value.strip()
    if len(cleaned) > maximum:
        raise MigrationFoundationError(f"{name} is too long")
    return cleaned


def require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MigrationFoundationError(f"{name} must be timezone-aware")
    return value


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
