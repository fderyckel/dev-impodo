"""Define mutable setup and workflow state inside one MigrationWorkspace.

Layer: domain/application boundary retained at the package root.

``WorkspaceStateService`` is called by workspace routers and persists through
the ``WorkspaceStateRepository`` port. It owns authorization, optimistic
revision checks, draft editability, and registration readiness. Concrete
persistence owns atomic invalidation when source, target, scope, or governance
changes affect later migration stages.

This module has no web-framework or database dependency. See
``docs/architecture/python-code-map.md``,
``docs/developer/contracts/project-lifecycle.md``, and ``tests/test_projects.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import StrEnum
import re
from urllib.parse import urlsplit
from typing import Protocol, Sequence
from uuid import UUID

from .access import Actor, AuthorizationPolicy, Capability


class WorkspaceStateError(ValueError):
    """Base error for invalid mutable workspace operations."""


class WorkspaceStateNotFoundError(WorkspaceStateError):
    """Raised when workspace engine state does not exist."""


class WorkspaceStateConflictError(WorkspaceStateError):
    """Raised when a stale browser form attempts to overwrite newer data."""


class WorkspaceStateCompatibilityError(WorkspaceStateError):
    """Raised when workspace state cannot be opened by this Impodo build."""


class WorkspaceRegistrationError(WorkspaceStateError):
    """Raised when a draft is incomplete and cannot be registered."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems))


class WorkspaceStatus(StrEnum):
    """Lifecycle state of the Stage A project setup boundary."""

    DRAFT = "DRAFT"
    REGISTERED = "REGISTERED"
    CLOSED = "CLOSED"


class ExportStatus(StrEnum):
    """Whether the declared source export is still planned or has been received."""

    PLANNED = "PLANNED"
    RECEIVED = "RECEIVED"


class SourceMode(StrEnum):
    """Select the governed origin used to create the project's source data."""

    FILE = "FILE"
    ODOO = "ODOO"


class DataClassification(StrEnum):
    """Govern retention, display, and operational handling of project data."""

    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class OdooConnectionMode(StrEnum):
    """Select the local-shell or remote JSON-2 read boundary for one project."""

    LOCAL = "LOCAL"
    REMOTE = "REMOTE"


class WorkspaceSetupStep(StrEnum):
    """Identify the setup page that owns one registration requirement."""

    DETAILS = "details"
    GOVERNANCE = "governance"
    FILES = "files"
    TARGET = "target"
    REVIEW = "review"


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
class WorkspaceSetupRequirement:
    """Describe one unmet setup requirement and its user-facing recovery."""

    code: str
    step: WorkspaceSetupStep
    problem: str
    guidance: str


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    """Hold the governed Stage A identity and current lifecycle pointers.

    The aggregate is immutable; service operations create a replacement with
    an incremented optimistic ``revision``. Source-file entries are immutable
    evidence, while mapping/run/approval fields are summaries whose underlying
    versioned evidence remains authoritative.
    """

    project_id: str
    name: str
    source_system: str
    source_mode: SourceMode = SourceMode.FILE
    export_status: ExportStatus = ExportStatus.PLANNED
    export_date: date | None = None
    description: str = ""
    data_manager: str = ""
    functional_owner: str = ""
    business_unit: str = ""
    data_classification: DataClassification = DataClassification.INTERNAL
    retention_days: int = 90
    support_access: bool = False
    odoo_connection_mode: OdooConnectionMode | None = None
    odoo_base_url: str = ""
    odoo_database: str = ""
    intended_applications: tuple[str, ...] = ()
    intended_models: tuple[str, ...] = ()
    source_files: tuple[SourceFile, ...] = ()
    status: WorkspaceStatus = WorkspaceStatus.DRAFT
    revision: int = 1
    created_at: datetime = field(default_factory=lambda: _now())
    updated_at: datetime = field(default_factory=lambda: _now())
    registered_at: datetime | None = None
    mapping_version: str | None = None
    current_run_id: str | None = None
    approval_status: ApprovalStatus = ApprovalStatus.NOT_STARTED

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_mode", SourceMode(self.source_mode))
        object.__setattr__(
            self,
            "approval_status",
            ApprovalStatus(self.approval_status),
        )


class WorkspaceStateRepository(Protocol):
    """Persistence port used by the workspace-state service."""

    def create_unlinked(self, project: WorkspaceState, *, actor: Actor) -> None:
        """Persist engine state for an existing MigrationWorkspace."""
        ...

    def get(self, project_id: str) -> WorkspaceState:
        """Return the complete current aggregate or raise ``WorkspaceStateNotFoundError``."""
        ...

    def assert_workspace_mutable(self, project_id: str) -> None:
        """Reject mutation when Recipe/DataVersion lifecycle seals the workspace."""
        ...

    def save(
        self,
        project: WorkspaceState,
        *,
        expected_revision: int,
        event_type: str,
        event_detail: str,
        actor: Actor,
    ) -> None:
        """Atomically save one optimistic lifecycle change and its audit event."""
        ...

    def add_source_file(
        self,
        project: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Attach immutable file evidence and invalidate affected current runs."""
        ...

    def remove_source_file(
        self,
        project: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Remove one unfrozen source and its file-scoped review evidence."""
        ...

    def update_schema_scope(
        self,
        project: WorkspaceState,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Replace the Stage C allowlist and invalidate schema dependents."""
        ...

    def record_credential_event(
        self,
        project_id: str,
        *,
        event_type: str,
        detail: str,
        actor: Actor,
    ) -> None:
        """Append one non-secret target-credential lifecycle event."""

        ...

    def record_credential_removal_receipt(
        self,
        *,
        receipt_hash: str,
        project_id: str,
        role: str,
        reason: str,
        connection_target_hash: str,
        credential_binding_hash: str | None,
        storage_class: str,
        removed_at: datetime,
        actor: Actor,
    ) -> None:
        """Persist a non-secret removal receipt outside project evidence."""

        ...


class WorkspaceStateService:
    """Own Stage A lifecycle operations independently of HTTP and DuckDB.

    Every mutation requires an actor capability and, after creation, the
    caller's expected project revision. Draft setup fields become read-only on
    registration; the permitted Odoo model scope is a separate Stage C
    decision and therefore remains changeable on registered projects.
    """

    def __init__(
        self,
        repository: WorkspaceStateRepository,
        authorization: AuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def provision_migration_workspace(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        name: str,
        source_system: str,
        source_mode: str | SourceMode,
        data_classification: str | DataClassification,
        retention_days: int,
    ) -> WorkspaceState:
        """Initialize mapping state for an existing clean MigrationWorkspace."""

        self.authorization.require(actor, Capability.MIGRATION_WORKSPACE_CREATE)
        workspace_id = _canonical_project_id(workspace_id)
        try:
            parsed_mode = SourceMode(source_mode)
            classification = DataClassification(data_classification)
        except ValueError as error:
            raise WorkspaceStateError("MigrationWorkspace setup values are invalid") from error
        if not 1 <= retention_days <= 3650:
            raise WorkspaceStateError("Retention must be between 1 and 3650 days")
        now = _now()
        workspace = WorkspaceState(
            project_id=workspace_id,
            name=_required_text(name, "Workspace name"),
            source_system=_required_text(source_system, "Source system"),
            source_mode=parsed_mode,
            description=f"Authoring workspace for {name.strip()}",
            data_classification=classification,
            retention_days=retention_days,
            created_at=now,
            updated_at=now,
        )
        self.repository.create_unlinked(workspace, actor=actor)
        return workspace

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
    ) -> WorkspaceState:
        """Validate and save editable source-export identity and description."""

        project = self._editable(
            project_id,
            expected_revision,
            actor=actor,
            capability=Capability.PROJECT_EDIT,
        )
        if project.source_mode is SourceMode.FILE:
            try:
                parsed_status = ExportStatus(export_status)
            except ValueError as error:
                raise WorkspaceStateError("Choose a valid export status") from error
            parsed_date = _optional_date(export_date)
            if parsed_status is ExportStatus.RECEIVED and parsed_date is None:
                raise WorkspaceStateError(
                    "Export date is required when files are received"
                )
            if parsed_date is not None and parsed_date > date.today():
                raise WorkspaceStateError("Export date cannot be in the future")
        else:
            parsed_status = ExportStatus.PLANNED
            parsed_date = None
        clean_description = description.strip()
        if len(clean_description) > 2000:
            raise WorkspaceStateError("Description is too long")
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
            "WORKSPACE_DETAILS_UPDATED",
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
    ) -> WorkspaceState:
        """Validate and save project ownership, classification, and retention."""

        project = self._editable(
            project_id,
            expected_revision,
            actor=actor,
            capability=Capability.PROJECT_EDIT,
        )
        if retention_days < 1 or retention_days > 3650:
            raise WorkspaceStateError("Retention must be between 1 and 3650 days")
        try:
            classification = DataClassification(data_classification)
        except ValueError as error:
            raise WorkspaceStateError("Choose a valid data classification") from error
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
            "WORKSPACE_GOVERNANCE_UPDATED",
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
    ) -> WorkspaceState:
        """Replace the draft target identity and application/model context.

        Concrete persistence invalidates current schema, mapping, and staging
        evidence when the saved target identity or scope actually changes.
        """

        project = self._target_editable(
            project_id,
            expected_revision,
            actor=actor,
        )
        try:
            connection_mode = OdooConnectionMode(odoo_connection_mode)
        except ValueError as error:
            raise WorkspaceStateError("Choose Local Odoo or Remote Odoo") from error
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
            "WORKSPACE_TARGET_UPDATED",
            actor=actor,
        )

    def _target_editable(
        self,
        project_id: str,
        expected_revision: int,
        *,
        actor: Actor,
    ) -> WorkspaceState:
        """Allow target setup after source registration.

        File projects intentionally defer their Odoo destination until the
        Odoo-data stage.  Concrete persistence already invalidates target-bound
        schema, mapping, and staging evidence when the identity changes.
        """

        _canonical_project_id(project_id)
        self.authorization.require(
            actor,
            Capability.PROJECT_EDIT,
            project_id=project_id,
        )
        project = self.repository.get(project_id)
        self.repository.assert_workspace_mutable(project.project_id)
        if project.revision != expected_revision:
            raise WorkspaceStateConflictError(
                "The project changed in another request; reload before continuing"
            )
        if project.status is WorkspaceStatus.CLOSED:
            raise WorkspaceStateError("Closed projects cannot be edited")
        return project

    def update_schema_scope(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        permitted_models: Sequence[str],
    ) -> WorkspaceState:
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
        self.repository.assert_workspace_mutable(project.project_id)
        if project.revision != expected_revision:
            raise WorkspaceStateConflictError(
                "The project changed in another request; reload before continuing"
            )
        if project.status is not WorkspaceStatus.REGISTERED:
            raise WorkspaceStateError(
                "Register the project before setting its permitted model scope"
            )
        models = _clean_choices(permitted_models)
        if not models:
            raise WorkspaceStateError("Add at least one permitted technical Odoo model")
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

    def record_credential_event(
        self,
        project_id: str,
        *,
        actor: Actor,
        role: str,
        action: str,
        binding_hash: str,
        persistent: bool,
    ) -> None:
        """Audit a successful vault mutation without recording its secret."""

        canonical_project_id = _canonical_project_id(project_id)
        normalized_role = role.strip().upper()
        normalized_action = action.strip().upper()
        if normalized_role not in {"READ", "WRITE"}:
            raise WorkspaceStateError("Credential audit role is invalid")
        if normalized_action not in {"STORED", "REPLACED"}:
            raise WorkspaceStateError("Credential audit action is invalid")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", binding_hash) is None:
            raise WorkspaceStateError("Credential audit binding is invalid")
        self.authorization.require(
            actor,
            (
                Capability.SCHEMA_DISCOVER
                if normalized_role == "READ"
                else Capability.EXPORT_PLAN_EXECUTE
            ),
            project_id=canonical_project_id,
        )
        self.repository.record_credential_event(
            canonical_project_id,
            event_type=(
                f"ODOO_{normalized_role}_CREDENTIAL_{normalized_action}"
            ),
            detail=(
                f"binding {binding_hash}; storage "
                f"{'OPERATING_SYSTEM_VAULT' if persistent else 'SESSION'}"
            ),
            actor=actor,
        )

    def record_credential_removal_receipt(
        self,
        *,
        receipt_hash: str,
        project_id: str,
        role: str,
        reason: str,
        connection_target_hash: str,
        credential_binding_hash: str | None,
        storage_class: str,
        removed_at: datetime,
        actor: Actor,
    ) -> None:
        """Retain one actor-bound receipt after a vault entry is removed."""

        canonical_project_id = _canonical_project_id(project_id)
        normalized_role = role.strip().upper()
        normalized_reason = reason.strip().upper()
        normalized_storage = storage_class.strip().upper()
        if normalized_role not in {"READ", "WRITE"}:
            raise WorkspaceStateError("Credential removal role is invalid")
        if normalized_reason not in {
            "TARGET_CHANGED",
            "RECIPE_DELETED",
            "USER_REQUESTED",
        }:
            raise WorkspaceStateError("Credential removal reason is invalid")
        if normalized_storage not in {
            "SESSION",
            "OPERATING_SYSTEM_VAULT",
            "UNKNOWN",
        }:
            raise WorkspaceStateError("Credential removal storage class is invalid")
        for value, label in (
            (receipt_hash, "receipt"),
            (connection_target_hash, "connection target"),
        ):
            if re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
                raise WorkspaceStateError(f"Credential removal {label} hash is invalid")
        if (
            credential_binding_hash is not None
            and re.fullmatch(
                r"sha256:[0-9a-f]{64}", credential_binding_hash
            )
            is None
        ):
            raise WorkspaceStateError("Credential removal binding hash is invalid")
        if removed_at.tzinfo is None:
            raise WorkspaceStateError("Credential removal time must be timezone-aware")
        self.authorization.require(
            actor,
            (
                Capability.RECIPE_DELETE
                if normalized_reason == "RECIPE_DELETED"
                else Capability.PROJECT_EDIT
            ),
            project_id=canonical_project_id,
        )
        self.repository.record_credential_removal_receipt(
            receipt_hash=receipt_hash,
            project_id=canonical_project_id,
            role=normalized_role,
            reason=normalized_reason,
            connection_target_hash=connection_target_hash,
            credential_binding_hash=credential_binding_hash,
            storage_class=normalized_storage,
            removed_at=removed_at,
            actor=actor,
        )

    def add_source_file(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        source_file: SourceFile,
    ) -> WorkspaceState:
        """Attach one source file before the project's tables are frozen."""

        project = self._source_files_editable(
            project_id,
            expected_revision,
            actor=actor,
        )
        if project.source_mode is not SourceMode.FILE:
            raise WorkspaceStateError("Odoo-source projects do not accept source files")
        if any(item.sha256 == source_file.sha256 for item in project.source_files):
            raise WorkspaceStateError("This exact source file is already registered")
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

    def remove_source_file(
        self,
        project_id: str,
        file_id: str,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> SourceFile:
        """Remove one source file before any table selection has been frozen."""

        project = self._source_files_editable(
            project_id,
            expected_revision,
            actor=actor,
        )
        if project.source_mode is not SourceMode.FILE:
            raise WorkspaceStateError("Odoo-source projects do not contain source files")
        source_file = next(
            (item for item in project.source_files if item.file_id == file_id),
            None,
        )
        if source_file is None:
            raise WorkspaceStateError("The selected source file is no longer in this project")
        saved = replace(
            project,
            source_files=tuple(
                item for item in project.source_files if item.file_id != file_id
            ),
            revision=project.revision + 1,
            updated_at=_now(),
        )
        self.repository.remove_source_file(
            saved,
            source_file,
            expected_revision=project.revision,
            actor=actor,
        )
        return source_file

    def _source_files_editable(
        self,
        project_id: str,
        expected_revision: int,
        *,
        actor: Actor,
    ) -> WorkspaceState:
        """Allow file-list amendments in draft or before registered table freeze."""

        _canonical_project_id(project_id)
        self.authorization.require(
            actor,
            Capability.PROJECT_EDIT,
            project_id=project_id,
        )
        project = self.repository.get(project_id)
        self.repository.assert_workspace_mutable(project.project_id)
        if project.revision != expected_revision:
            raise WorkspaceStateConflictError(
                "The project changed in another request; reload before continuing"
            )
        if project.status is WorkspaceStatus.CLOSED:
            raise WorkspaceStateError("Closed projects cannot be edited")
        return project

    def register(
        self,
        project_id: str,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> WorkspaceState:
        """Register a complete draft and close the editable setup boundary.

        All problems from :func:`workspace_registration_problems` are returned together
        through ``WorkspaceRegistrationError``. Registration is project evidence,
        not mapping, normalization, package, or execution approval.
        """

        project = self._editable(
            project_id,
            expected_revision,
            actor=actor,
            capability=Capability.PROJECT_REGISTER,
        )
        problems = workspace_registration_problems(project)
        if problems:
            raise WorkspaceRegistrationError(problems)
        registered = replace(
            project,
            status=WorkspaceStatus.REGISTERED,
            registered_at=_now(),
        )
        return self._save(
            registered,
            project,
            "WORKSPACE_REGISTERED",
            actor=actor,
        )

    def _editable(
        self,
        project_id: str,
        expected_revision: int,
        *,
        actor: Actor,
        capability: Capability,
    ) -> WorkspaceState:
        _canonical_project_id(project_id)
        self.authorization.require(
            actor,
            capability,
            project_id=project_id,
        )
        project = self.repository.get(project_id)
        self.repository.assert_workspace_mutable(project.project_id)
        if project.revision != expected_revision:
            raise WorkspaceStateConflictError(
                "The project changed in another request; reload before continuing"
            )
        if project.status is not WorkspaceStatus.DRAFT:
            raise WorkspaceStateError("Registered or closed projects cannot be edited")
        return project

    def _save(
        self,
        updated: WorkspaceState,
        previous: WorkspaceState,
        event_type: str,
        *,
        actor: Actor,
        detail: str = "",
    ) -> WorkspaceState:
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


def workspace_setup_requirements(
    project: WorkspaceState,
) -> tuple[WorkspaceSetupRequirement, ...]:
    """Return every unmet setup requirement with one owning browser step."""

    requirements: list[WorkspaceSetupRequirement] = []
    if not project.name:
        requirements.append(
            WorkspaceSetupRequirement(
                code="WORKSPACE_NAME_REQUIRED",
                step=WorkspaceSetupStep.DETAILS,
                problem="Project name is required",
                guidance="Enter a project name.",
            )
        )
    if not project.source_system:
        requirements.append(
            WorkspaceSetupRequirement(
                code="SOURCE_SYSTEM_REQUIRED",
                step=WorkspaceSetupStep.DETAILS,
                problem="Source system is required",
                guidance="Choose the source system.",
            )
        )
    if project.source_mode is SourceMode.FILE:
        if not project.source_files:
            requirements.append(
                WorkspaceSetupRequirement(
                    code="SOURCE_FILE_REQUIRED",
                    step=WorkspaceSetupStep.FILES,
                    problem="At least one source file is required",
                    guidance="Add at least one source file.",
                )
            )
    elif project.source_files:
        requirements.append(
            WorkspaceSetupRequirement(
                code="ODOO_SOURCE_FILES_NOT_ALLOWED",
                step=WorkspaceSetupStep.DETAILS,
                problem="Odoo-source projects cannot contain source files",
                guidance=(
                    "This data version uses Odoo records and cannot also use "
                    "uploaded source files."
                ),
            )
        )
    if (
        project.source_mode is SourceMode.ODOO
        and project.odoo_connection_mode is None
    ):
        requirements.append(
            WorkspaceSetupRequirement(
                code="ODOO_CONNECTION_MODE_REQUIRED",
                step=WorkspaceSetupStep.TARGET,
                problem="Choose a Local Odoo or Remote Odoo connection",
                guidance="Choose where Odoo is running.",
            )
        )
    if project.source_mode is SourceMode.ODOO and not project.odoo_base_url:
        requirements.append(
            WorkspaceSetupRequirement(
                code="ODOO_BASE_URL_REQUIRED",
                step=WorkspaceSetupStep.TARGET,
                problem="Odoo base URL is required",
                guidance="Enter the Odoo web address.",
            )
        )
    if project.source_mode is SourceMode.ODOO and not project.odoo_database:
        requirements.append(
            WorkspaceSetupRequirement(
                code="ODOO_DATABASE_REQUIRED",
                step=WorkspaceSetupStep.TARGET,
                problem="Odoo database is required",
                guidance="Enter the Odoo database name.",
            )
        )
    return tuple(requirements)


def workspace_setup_requirements_for_step(
    project: WorkspaceState,
    step: WorkspaceSetupStep,
) -> tuple[WorkspaceSetupRequirement, ...]:
    """Return only the unmet requirements owned by ``step``."""

    return tuple(
        requirement
        for requirement in workspace_setup_requirements(project)
        if requirement.step is step
    )


def workspace_registration_problems(project: WorkspaceState) -> tuple[str, ...]:
    """Return every user-actionable reason a draft cannot be registered."""

    return tuple(
        requirement.problem
        for requirement in workspace_setup_requirements(project)
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _required_text(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise WorkspaceStateError(f"{label} is required")
    if len(cleaned) > 200:
        raise WorkspaceStateError(f"{label} is too long")
    return cleaned


def _optional_text(value: str, label: str) -> str:
    cleaned = value.strip()
    if len(cleaned) > 200:
        raise WorkspaceStateError(f"{label} is too long")
    return cleaned


def _optional_date(value: str) -> date | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError as error:
        raise WorkspaceStateError("Export date must be a valid date") from error


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
        raise WorkspaceStateError("The Odoo URL contains an invalid port") from error
    if (
        not parsed_url.hostname
        or parsed_url.username
        or parsed_url.password
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise WorkspaceStateError(
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
            raise WorkspaceStateError(
                "Local Odoo must use http://127.0.0.1:<port> or "
                "http://[::1]:<port> without an extra path"
            )
    elif parsed_url.scheme != "https" or is_literal_loopback or hostname == "localhost":
        raise WorkspaceStateError(
            "Remote Odoo must use an HTTPS server URL; choose Local Odoo "
            "for a loopback instance"
        )
    return base_url


def _canonical_project_id(project_id: str) -> str:
    try:
        return str(UUID(project_id))
    except (ValueError, AttributeError) as error:
        raise WorkspaceStateNotFoundError("Invalid project identifier") from error

