"""Define mutable target setup owned by one MigrationRun."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Sequence
from urllib.parse import urlsplit

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.project.foundation import (
    MigrationFoundationError,
    optional_text,
    require_aware,
    require_revision,
    require_uuid,
    utc_now,
)


class OdooConnectionMode(StrEnum):
    """Select the local-shell or remote JSON-2 boundary for one run target."""

    LOCAL = "LOCAL"
    REMOTE = "REMOTE"


@dataclass(frozen=True, slots=True)
class MigrationRunTargetSetup:
    """Hold the mutable target choice before an immutable TargetBinding exists."""

    migration_run_id: str
    project_id: str
    revision: int
    connection_mode: OdooConnectionMode
    base_url: str
    database: str
    intended_applications: tuple[str, ...]
    updated_at: datetime

    def __post_init__(self) -> None:
        require_uuid(self.migration_run_id, "migration_run_id")
        require_uuid(self.project_id, "project_id")
        require_revision(self.revision, "target_setup_revision")
        object.__setattr__(
            self,
            "connection_mode",
            OdooConnectionMode(self.connection_mode),
        )
        object.__setattr__(
            self,
            "base_url",
            validate_odoo_base_url(self.base_url, self.connection_mode),
        )
        object.__setattr__(
            self,
            "database",
            optional_text(self.database, "Odoo database", maximum=200),
        )
        object.__setattr__(
            self,
            "intended_applications",
            clean_target_choices(self.intended_applications),
        )
        require_aware(self.updated_at, "updated_at")


class MigrationRunTargetSetupRepository(Protocol):
    def migration_run_project_id(self, migration_run_id: str) -> str: ...

    def get_migration_run_target_setup(
        self,
        migration_run_id: str,
    ) -> MigrationRunTargetSetup | None: ...

    def migration_run_is_mutable(self, migration_run_id: str) -> bool: ...

    def replace_migration_run_target_setup(
        self,
        setup: MigrationRunTargetSetup,
        *,
        expected_revision: int | None,
        actor: Actor,
    ) -> MigrationRunTargetSetup: ...


class MigrationRunTargetSetupService:
    """Authorize and replace one run-owned mutable target choice."""

    def __init__(
        self,
        repository: MigrationRunTargetSetupRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def get(
        self,
        migration_run_id: str,
        *,
        actor: Actor,
    ) -> MigrationRunTargetSetup | None:
        self.authorization.require(actor, Capability.PROJECT_VIEW)
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        project_id = self.repository.migration_run_project_id(migration_run_id)
        self.authorization.require(
            actor,
            Capability.PROJECT_VIEW,
            project_id=project_id,
        )
        return self.repository.get_migration_run_target_setup(migration_run_id)

    def replace(
        self,
        migration_run_id: str,
        *,
        actor: Actor,
        expected_revision: int | None,
        connection_mode: str | OdooConnectionMode,
        base_url: str,
        database: str,
        intended_applications: Sequence[str],
    ) -> MigrationRunTargetSetup:
        self.authorization.require(actor, Capability.MIGRATION_RUN_EDIT)
        migration_run_id = require_uuid(migration_run_id, "migration_run_id")
        project_id = self.repository.migration_run_project_id(migration_run_id)
        self.authorization.require(
            actor,
            Capability.MIGRATION_RUN_EDIT,
            project_id=project_id,
        )
        if not self.repository.migration_run_is_mutable(migration_run_id):
            raise MigrationFoundationError(
                "A completed MigrationRun target is historical evidence"
            )
        current = self.repository.get_migration_run_target_setup(migration_run_id)
        if current is None:
            if expected_revision is not None:
                raise MigrationFoundationError(
                    "MigrationRun target setup changed; reload and retry"
                )
            revision = 1
        else:
            if expected_revision != current.revision:
                raise MigrationFoundationError(
                    "MigrationRun target setup changed; reload and retry"
                )
            revision = current.revision + 1
        setup = MigrationRunTargetSetup(
            migration_run_id=migration_run_id,
            project_id=project_id,
            revision=revision,
            connection_mode=OdooConnectionMode(connection_mode),
            base_url=base_url,
            database=database,
            intended_applications=tuple(intended_applications),
            updated_at=utc_now(),
        )
        if current is not None and (
            setup.connection_mode == current.connection_mode
            and setup.base_url == current.base_url
            and setup.database == current.database
            and setup.intended_applications == current.intended_applications
        ):
            return current
        return self.repository.replace_migration_run_target_setup(
            setup,
            expected_revision=expected_revision,
            actor=actor,
        )


def clean_target_choices(values: Sequence[str]) -> tuple[str, ...]:
    cleaned = {value.strip() for value in values if value.strip()}
    return tuple(sorted(cleaned, key=str.casefold))


def validate_odoo_base_url(
    value: str,
    connection_mode: OdooConnectionMode,
) -> str:
    """Return one credential-free local or remote Odoo base URL."""

    base_url = value.strip().rstrip("/")
    if not base_url:
        return ""
    try:
        parsed_url = urlsplit(base_url)
        parsed_url.port
    except ValueError as error:
        raise MigrationFoundationError(
            "The Odoo URL contains an invalid port"
        ) from error
    if (
        not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise MigrationFoundationError(
            "The Odoo URL cannot contain credentials, query parameters, or fragments"
        )
    hostname = parsed_url.hostname.casefold()
    is_literal_loopback = hostname in {"127.0.0.1", "::1"}
    if connection_mode is OdooConnectionMode.LOCAL:
        if (
            parsed_url.scheme not in {"http", "https"}
            or not is_literal_loopback
            or parsed_url.path not in {"", "/"}
        ):
            raise MigrationFoundationError(
                "Local Odoo must use a literal loopback URL without an extra path"
            )
    elif (
        parsed_url.scheme != "https"
        or is_literal_loopback
        or hostname == "localhost"
    ):
        raise MigrationFoundationError(
            "Remote Odoo must use HTTPS; choose Local Odoo for a loopback instance"
        )
    return base_url
