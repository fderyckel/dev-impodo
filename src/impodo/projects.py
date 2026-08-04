"""Migration-project domain for the local Impodo application.

This module has no web-framework or database dependency.  The browser, CLI,
and persistence adapters all use the same lifecycle and validation rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import StrEnum
from urllib.parse import urlsplit
from typing import Protocol, Sequence
from uuid import UUID, uuid4

from .access import Actor, AuthorizationPolicy, Capability


class ProjectError(ValueError):
    """Base error for invalid project operations."""


class ProjectNotFoundError(ProjectError):
    """Raised when a project identifier does not exist."""


class ProjectConflictError(ProjectError):
    """Raised when a stale browser form attempts to overwrite newer data."""


class ProjectRegistrationError(ProjectError):
    """Raised when a draft is incomplete and cannot be registered."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


class ProjectStatus(StrEnum):
    DRAFT = "DRAFT"
    REGISTERED = "REGISTERED"
    CLOSED = "CLOSED"


class ExportStatus(StrEnum):
    PLANNED = "PLANNED"
    RECEIVED = "RECEIVED"


class DataClassification(StrEnum):
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class OdooConnectionMode(StrEnum):
    LOCAL = "LOCAL"
    REMOTE = "REMOTE"


class ApprovalStatus(StrEnum):
    """Derived summary; immutable approval evidence remains authoritative."""

    NOT_STARTED = "NOT_STARTED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, slots=True)
class SourceFile:
    """Immutable evidence for one governed source file."""

    file_id: str
    display_name: str
    stored_name: str
    size_bytes: int
    sha256: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectSummary:
    project_id: str
    name: str
    status: ProjectStatus
    revision: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MigrationProject:
    """Project-setup migration-project aggregate."""

    project_id: str
    name: str
    source_system: str
    export_status: ExportStatus = ExportStatus.PLANNED
    export_date: date | None = None
    description: str = ""
    data_manager: str = ""
    functional_owner: str = ""
    business_unit: str = ""
    data_classification: DataClassification = DataClassification.CONFIDENTIAL
    retention_days: int = 90
    support_access: bool = False
    odoo_connection_mode: OdooConnectionMode | None = None
    odoo_base_url: str = ""
    odoo_database: str = ""
    intended_applications: tuple[str, ...] = ()
    intended_models: tuple[str, ...] = ()
    source_files: tuple[SourceFile, ...] = ()
    status: ProjectStatus = ProjectStatus.DRAFT
    revision: int = 1
    created_at: datetime = field(default_factory=lambda: _now())
    updated_at: datetime = field(default_factory=lambda: _now())
    registered_at: datetime | None = None
    mapping_version: str | None = None
    current_run_id: str | None = None
    approval_status: ApprovalStatus = ApprovalStatus.NOT_STARTED

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "approval_status",
            ApprovalStatus(self.approval_status),
        )


class ProjectRepository(Protocol):
    """Persistence port used by the project application service."""

    def create(self, project: MigrationProject, *, actor: Actor) -> None: ...

    def get(self, project_id: str) -> MigrationProject: ...

    def list(self) -> tuple[ProjectSummary, ...]: ...

    def delete(
        self,
        project_id: str,
        *,
        expected_revision: int,
    ) -> None: ...

    def save(
        self,
        project: MigrationProject,
        *,
        expected_revision: int,
        event_type: str,
        event_detail: str,
        actor: Actor,
    ) -> None: ...

    def add_source_file(
        self,
        project: MigrationProject,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None: ...

    def update_schema_scope(
        self,
        project: MigrationProject,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None: ...


class ProjectService:
    """Own project lifecycle operations independently of HTTP and DuckDB."""

    def __init__(
        self,
        repository: ProjectRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def create_project(
        self,
        *,
        actor: Actor,
        name: str,
        source_system: str,
    ) -> MigrationProject:
        self.authorization.require(actor, Capability.PROJECT_CREATE)
        clean_name = _required_text(name, "Project name")
        clean_source = _required_text(source_system, "Source system")
        now = _now()
        project = MigrationProject(
            project_id=str(uuid4()),
            name=clean_name,
            source_system=clean_source,
            created_at=now,
            updated_at=now,
        )
        self.repository.create(project, actor=actor)
        return project

    def delete_project(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> MigrationProject:
        """Permanently delete one project regardless of lifecycle status."""

        canonical_project_id = _canonical_project_id(project_id)
        self.authorization.require(
            actor,
            Capability.PROJECT_DELETE,
            project_id=canonical_project_id,
        )
        project = self.repository.get(canonical_project_id)
        if project.revision != expected_revision:
            raise ProjectConflictError(
                "The project changed in another request; reload before deleting"
            )
        self.repository.delete(
            canonical_project_id,
            expected_revision=expected_revision,
        )
        return project

    def update_details(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        name: str,
        source_system: str,
        export_status: str,
        export_date: str,
        description: str,
    ) -> MigrationProject:
        project = self._editable(
            project_id,
            expected_revision,
            actor=actor,
            capability=Capability.PROJECT_EDIT,
        )
        try:
            parsed_status = ExportStatus(export_status)
        except ValueError as error:
            raise ProjectError("Choose a valid export status") from error
        parsed_date = _optional_date(export_date)
        if parsed_status is ExportStatus.RECEIVED and parsed_date is None:
            raise ProjectError("Export date is required when files are received")
        if parsed_date is not None and parsed_date > date.today():
            raise ProjectError("Export date cannot be in the future")
        clean_description = description.strip()
        if len(clean_description) > 2000:
            raise ProjectError("Description is too long")
        updated = replace(
            project,
            name=_required_text(name, "Project name"),
            source_system=_required_text(source_system, "Source system"),
            export_status=parsed_status,
            export_date=parsed_date,
            description=clean_description,
        )
        return self._save(
            updated,
            project,
            "PROJECT_DETAILS_UPDATED",
            actor=actor,
        )

    def update_governance(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        data_manager: str,
        functional_owner: str,
        business_unit: str,
        data_classification: str,
        retention_days: int,
        support_access: bool,
    ) -> MigrationProject:
        project = self._editable(
            project_id,
            expected_revision,
            actor=actor,
            capability=Capability.PROJECT_EDIT,
        )
        if retention_days < 1 or retention_days > 3650:
            raise ProjectError("Retention must be between 1 and 3650 days")
        try:
            classification = DataClassification(data_classification)
        except ValueError as error:
            raise ProjectError("Choose a valid data classification") from error
        updated = replace(
            project,
            data_manager=_optional_text(data_manager, "Data manager"),
            functional_owner=_optional_text(functional_owner, "Functional owner"),
            business_unit=_optional_text(business_unit, "Business unit"),
            data_classification=classification,
            retention_days=retention_days,
            support_access=support_access,
        )
        return self._save(
            updated,
            project,
            "PROJECT_GOVERNANCE_UPDATED",
            actor=actor,
        )

    def update_target(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        odoo_connection_mode: str,
        odoo_base_url: str,
        odoo_database: str,
        intended_applications: Sequence[str],
        intended_models: Sequence[str] | None = None,
    ) -> MigrationProject:
        project = self._editable(
            project_id,
            expected_revision,
            actor=actor,
            capability=Capability.PROJECT_EDIT,
        )
        try:
            connection_mode = OdooConnectionMode(odoo_connection_mode)
        except ValueError as error:
            raise ProjectError("Choose Local Odoo or Remote Odoo") from error
        base_url = _validated_odoo_base_url(odoo_base_url, connection_mode)
        database = _optional_text(odoo_database, "Odoo database")
        updated = replace(
            project,
            odoo_connection_mode=connection_mode,
            odoo_base_url=base_url,
            odoo_database=database,
            intended_applications=_clean_choices(intended_applications),
            intended_models=(
                _clean_choices(intended_models)
                if intended_models is not None
                else project.intended_models
            ),
        )
        return self._save(
            updated,
            project,
            "PROJECT_TARGET_UPDATED",
            actor=actor,
        )

    def update_schema_scope(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        permitted_models: Sequence[str],
    ) -> MigrationProject:
        """Set the exact Odoo models Stage C may read and map.

        This deliberately remains available after project registration. It is
        a schema-discovery decision, rather than a change to the registered
        Odoo target or the project's business context.
        """

        _canonical_project_id(project_id)
        self.authorization.require(
            actor,
            Capability.SCHEMA_DISCOVER,
            project_id=project_id,
        )
        project = self.repository.get(project_id)
        if project.revision != expected_revision:
            raise ProjectConflictError(
                "The project changed in another request; reload before continuing"
            )
        if project.status is not ProjectStatus.REGISTERED:
            raise ProjectError(
                "Register the project before setting its permitted model scope"
            )
        models = _clean_choices(permitted_models)
        if not models:
            raise ProjectError("Add at least one permitted technical Odoo model")
        if models == project.intended_models:
            return project
        updated = replace(
            project,
            intended_models=models,
            mapping_version=None,
            approval_status=(
                ApprovalStatus.INVALIDATED
                if project.mapping_version
                else project.approval_status
            ),
        )
        saved = replace(
            updated,
            revision=project.revision + 1,
            updated_at=_now(),
        )
        self.repository.update_schema_scope(
            saved,
            expected_revision=project.revision,
            actor=actor,
        )
        return saved

    def add_source_file(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        source_file: SourceFile,
    ) -> MigrationProject:
        project = self._editable(
            project_id,
            expected_revision,
            actor=actor,
            capability=Capability.PROJECT_EDIT,
        )
        if any(item.sha256 == source_file.sha256 for item in project.source_files):
            raise ProjectError("This exact source file is already registered")
        updated = replace(project, source_files=project.source_files + (source_file,))
        saved = replace(
            updated,
            revision=project.revision + 1,
            updated_at=_now(),
        )
        self.repository.add_source_file(
            saved,
            source_file,
            expected_revision=project.revision,
            actor=actor,
        )
        return saved

    def register(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> MigrationProject:
        project = self._editable(
            project_id,
            expected_revision,
            actor=actor,
            capability=Capability.PROJECT_REGISTER,
        )
        problems = registration_problems(project)
        if problems:
            raise ProjectRegistrationError(problems)
        registered = replace(
            project,
            status=ProjectStatus.REGISTERED,
            registered_at=_now(),
        )
        return self._save(
            registered,
            project,
            "PROJECT_REGISTERED",
            actor=actor,
        )

    def _editable(
        self,
        project_id: str,
        expected_revision: int,
        *,
        actor: Actor,
        capability: Capability,
    ) -> MigrationProject:
        _canonical_project_id(project_id)
        self.authorization.require(
            actor,
            capability,
            project_id=project_id,
        )
        project = self.repository.get(project_id)
        if project.revision != expected_revision:
            raise ProjectConflictError(
                "The project changed in another request; reload before continuing"
            )
        if project.status is not ProjectStatus.DRAFT:
            raise ProjectError("Registered or closed projects cannot be edited")
        return project

    def _save(
        self,
        updated: MigrationProject,
        previous: MigrationProject,
        event_type: str,
        *,
        actor: Actor,
        detail: str = "",
    ) -> MigrationProject:
        saved = replace(
            updated,
            revision=previous.revision + 1,
            updated_at=_now(),
        )
        self.repository.save(
            saved,
            expected_revision=previous.revision,
            event_type=event_type,
            event_detail=detail,
            actor=actor,
        )
        return saved


def registration_problems(project: MigrationProject) -> tuple[str, ...]:
    """Return every user-actionable reason a draft cannot be registered."""

    problems: list[str] = []
    if not project.name:
        problems.append("Project name is required")
    if not project.source_system:
        problems.append("Source system is required")
    if project.export_status is not ExportStatus.RECEIVED:
        problems.append("Source export must be marked as received")
    if project.export_date is None:
        problems.append("Source export date is required")
    if not project.source_files:
        problems.append("At least one source file is required")
    if not project.data_manager:
        problems.append("Responsible data manager is required")
    if not project.functional_owner:
        problems.append("Functional owner is required")
    if project.odoo_connection_mode is None:
        problems.append("Choose a Local Odoo or Remote Odoo connection")
    if not project.odoo_base_url:
        problems.append("Odoo base URL is required")
    if not project.odoo_database:
        problems.append("Odoo database is required")
    return tuple(problems)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required_text(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ProjectError(f"{label} is required")
    if len(cleaned) > 200:
        raise ProjectError(f"{label} is too long")
    return cleaned


def _optional_text(value: str, label: str) -> str:
    cleaned = value.strip()
    if len(cleaned) > 200:
        raise ProjectError(f"{label} is too long")
    return cleaned


def _optional_date(value: str) -> date | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError as error:
        raise ProjectError("Export date must be a valid date") from error


def _clean_choices(values: Sequence[str]) -> tuple[str, ...]:
    cleaned = {value.strip() for value in values if value.strip()}
    return tuple(sorted(cleaned, key=str.casefold))


def _validated_odoo_base_url(
    value: str,
    connection_mode: OdooConnectionMode,
) -> str:
    base_url = value.strip().rstrip("/")
    if not base_url:
        return ""
    try:
        parsed_url = urlsplit(base_url)
        parsed_url.port
    except ValueError as error:
        raise ProjectError("The Odoo URL contains an invalid port") from error
    if (
        not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise ProjectError(
            "The Odoo URL cannot contain credentials, query parameters, "
            "or fragments"
        )

    hostname = parsed_url.hostname.casefold()
    is_literal_loopback = hostname in {"127.0.0.1", "::1"}
    if connection_mode is OdooConnectionMode.LOCAL:
        if (
            parsed_url.scheme not in {"http", "https"}
            or not is_literal_loopback
            or parsed_url.path not in {"", "/"}
        ):
            raise ProjectError(
                "Local Odoo must use http://127.0.0.1:<port> or "
                "http://[::1]:<port> without an extra path"
            )
    elif parsed_url.scheme != "https" or is_literal_loopback or hostname == "localhost":
        raise ProjectError(
            "Remote Odoo must use an HTTPS server URL; choose Local Odoo "
            "for a loopback instance"
        )
    return base_url


def _canonical_project_id(project_id: str) -> str:
    try:
        return str(UUID(project_id))
    except (ValueError, AttributeError) as error:
        raise ProjectNotFoundError("Invalid project identifier") from error
