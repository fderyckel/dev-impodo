"""Expose the flat workbench command projection for one MigrationWorkspace.

Layer: domain/application boundary retained at the package root.

``WorkspaceStateService`` is called by workspace routers and persists through
the ``WorkspaceStateRepository`` port. Canonical Project values, Data version
source values, Migration run target values, and MigrationWorkspace setup state
are owned outside this projection. The concrete repository composes those
owners and keeps only current workspace-engine evidence and derived caches.

This module has no web-framework or database dependency. See
``docs/architecture/python-code-map.md``,
``docs/developer/contracts/project-lifecycle.md``,
``tests/integration/duckdb/test_workspace.py``, and
``tests/architecture/test_canonical_ownership.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Protocol, Sequence
from uuid import UUID

from impodo.domain.shared.access import Actor, Capability, WorkspaceAuthorizationPolicy
from impodo.domain.run.setup import OdooConnectionMode, validate_odoo_base_url
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.destination_matching import DestinationMatchPlan
from impodo.domain.workspace.transfer_order import TransferOrderPlan


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
    """Lifecycle state of the Stage A workspace setup boundary."""

    DRAFT = "DRAFT"
    REGISTERED = "REGISTERED"
    CLOSED = "CLOSED"


class SourceMode(StrEnum):
    """Select the governed origin used to create the workspace's source data."""

    FILE = "FILE"
    ODOO = "ODOO"


class DataClassification(StrEnum):
    """Govern retention, display, and operational handling of workspace data."""

    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    RESTRICTED = "RESTRICTED"


class WorkspaceSetupStep(StrEnum):
    """Identify the setup page that owns one registration requirement."""

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
    """Project canonical owners into the retained workspace workbench.

    This flat read-and-command model serves the contained mapping engine. It is
    not an identity or aggregate root. ``MigrationProject``,
    ``MigrationWorkspace``, the DataVersion package, and ``MigrationRun`` own
    the values projected here; the workbench owns only its local evidence.
    """

    workspace_id: str
    name: str
    source_system: str
    source_mode: SourceMode = SourceMode.FILE
    data_classification: DataClassification = DataClassification.INTERNAL
    retention_days: int = 90
    odoo_connection_mode: OdooConnectionMode | None = None
    odoo_base_url: str = ""
    odoo_database: str = ""
    destination_odoo_connection_mode: OdooConnectionMode | None = None
    destination_odoo_base_url: str = ""
    destination_odoo_database: str = ""
    destination_verified_target_hash: str = ""
    destination_verified_credential_binding_hash: str = ""
    destination_verified_read_principal_hash: str = ""
    destination_verified_odoo_version: str = ""
    destination_verified_at: datetime | None = None
    destination_match_plan: DestinationMatchPlan | None = None
    transfer_order_plan: TransferOrderPlan | None = None
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
        if self.destination_odoo_connection_mode is not None:
            object.__setattr__(
                self,
                "destination_odoo_connection_mode",
                OdooConnectionMode(self.destination_odoo_connection_mode),
            )
        object.__setattr__(
            self,
            "approval_status",
            ApprovalStatus(self.approval_status),
        )

    @property
    def destination_configured(self) -> bool:
        """Whether a separate Odoo destination identity is configured."""

        return bool(
            self.destination_odoo_connection_mode
            and self.destination_odoo_base_url
            and self.destination_odoo_database
        )

    @property
    def destination_verified(self) -> bool:
        """Whether current destination details match durable read evidence."""

        return bool(
            self.destination_configured
            and not transfer_destination_matches_source(self)
            and self.destination_verified_target_hash
            == transfer_destination_identity_hash(self)
            and self.destination_verified_credential_binding_hash
            and self.destination_verified_read_principal_hash
            and self.destination_verified_odoo_version.startswith("19.")
            and self.destination_verified_at is not None
        )

    def destination_match_current(
        self,
        *,
        source_selection_hash: str,
        source_schema_hash: str,
    ) -> bool:
        """Whether Stage 5 evidence matches every current upstream binding."""

        plan = self.destination_match_plan
        return bool(
            self.destination_verified
            and plan is not None
            and plan.workspace_id == self.workspace_id
            and plan.source_selection_hash == source_selection_hash
            and plan.source_schema_hash == source_schema_hash
            and plan.destination_target_hash
            == transfer_destination_identity_hash(self)
            and plan.destination_credential_binding_hash
            == self.destination_verified_credential_binding_hash
            and plan.destination_read_principal_hash
            == self.destination_verified_read_principal_hash
            and self.destination_verified_at is not None
            and plan.recorded_at >= self.destination_verified_at
        )

    def destination_match_ready(
        self,
        *,
        source_selection_hash: str,
        source_schema_hash: str,
    ) -> bool:
        """Whether current Stage 5 evidence has no matching blockers."""

        return bool(
            self.destination_match_current(
                source_selection_hash=source_selection_hash,
                source_schema_hash=source_schema_hash,
            )
            and self.destination_match_plan is not None
            and self.destination_match_plan.ready
        )

    def transfer_order_current(
        self,
        *,
        source_selection_hash: str,
        source_schema_hash: str,
    ) -> bool:
        """Whether Stage 6 is bound to the exact current Stage 5 decision."""

        plan = self.transfer_order_plan
        match_plan = self.destination_match_plan
        return bool(
            self.destination_match_ready(
                source_selection_hash=source_selection_hash,
                source_schema_hash=source_schema_hash,
            )
            and plan is not None
            and match_plan is not None
            and plan.workspace_id == self.workspace_id
            and plan.destination_match_plan_hash == match_plan.content_hash
            and plan.source_selection_hash == source_selection_hash
            and plan.source_schema_hash == source_schema_hash
            and plan.destination_target_hash
            == transfer_destination_identity_hash(self)
            and plan.recorded_at >= match_plan.recorded_at
        )

    def transfer_order_ready(
        self,
        *,
        source_selection_hash: str,
        source_schema_hash: str,
    ) -> bool:
        """Whether current Stage 6 evidence has no dependency blockers."""

        return bool(
            self.transfer_order_current(
                source_selection_hash=source_selection_hash,
                source_schema_hash=source_schema_hash,
            )
            and self.transfer_order_plan is not None
            and self.transfer_order_plan.ready
        )


class WorkspaceStateRepository(Protocol):
    """Persistence port used by the workspace-state service."""

    def initialize_workbench(
        self,
        workspace: WorkspaceState,
        *,
        actor: Actor,
    ) -> None:
        """Persist engine state for an existing MigrationWorkspace."""
        ...

    def get(self, workspace_id: str) -> WorkspaceState:
        """Return the complete current aggregate or raise ``WorkspaceStateNotFoundError``."""
        ...

    def assert_workspace_mutable(self, workspace_id: str) -> None:
        """Reject mutation when Recipe/DataVersion lifecycle seals the workspace."""
        ...

    def save(
        self,
        workspace: WorkspaceState,
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
        workspace: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Attach immutable file evidence and invalidate affected current runs."""
        ...

    def remove_source_file(
        self,
        workspace: WorkspaceState,
        source_file: SourceFile,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Remove one unfrozen source and its file-scoped review evidence."""
        ...

    def update_schema_scope(
        self,
        workspace: WorkspaceState,
        *,
        expected_revision: int,
        actor: Actor,
    ) -> None:
        """Replace the Stage C allowlist and invalidate schema dependents."""
        ...

    def record_credential_event(
        self,
        workspace_id: str,
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
        workspace_id: str,
        role: str,
        reason: str,
        connection_target_hash: str,
        credential_binding_hash: str | None,
        storage_class: str,
        removed_at: datetime,
        actor: Actor,
    ) -> None:
        """Persist a non-secret removal receipt outside workspace evidence."""

        ...


class WorkspaceStateService:
    """Provide the retained workbench commands independently of HTTP and DuckDB.

    Every mutation requires an actor capability and an optimistic workbench
    revision where applicable. Registration advances the canonical
    MigrationWorkspace setup root. The permitted Odoo model scope remains
    changeable after registration because it is a separate mapping decision.
    """

    def __init__(
        self,
        repository: WorkspaceStateRepository,
        authorization: WorkspaceAuthorizationPolicy,
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

        workspace_id = _canonical_workspace_id(workspace_id)
        self.authorization.require(
            actor,
            Capability.MIGRATION_WORKSPACE_CREATE,
            workspace_id=workspace_id,
        )
        try:
            parsed_mode = SourceMode(source_mode)
            classification = DataClassification(data_classification)
        except ValueError as error:
            raise WorkspaceStateError("MigrationWorkspace setup values are invalid") from error
        if not 1 <= retention_days <= 3650:
            raise WorkspaceStateError("Retention must be between 1 and 3650 days")
        now = _now()
        workspace = WorkspaceState(
            workspace_id=workspace_id,
            name=_required_text(name, "Workspace name"),
            source_system=_required_text(source_system, "Source system"),
            source_mode=parsed_mode,
            data_classification=classification,
            retention_days=retention_days,
            created_at=now,
            updated_at=now,
        )
        self.repository.initialize_workbench(workspace, actor=actor)
        return workspace

    def update_target(
        self,
        workspace_id: str,
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

        workspace = self._target_editable(
            workspace_id,
            expected_revision,
            actor=actor,
        )
        try:
            connection_mode = OdooConnectionMode(odoo_connection_mode)
        except ValueError as error:
            raise WorkspaceStateError("Choose Local Odoo or Remote Odoo") from error
        try:
            base_url = validate_odoo_base_url(odoo_base_url, connection_mode)
        except ValueError as error:
            raise WorkspaceStateError(str(error)) from error
        database = _optional_text(odoo_database, "Odoo database")
        updated = replace(
            workspace,
            odoo_connection_mode=connection_mode,
            odoo_base_url=base_url,
            odoo_database=database,
            intended_applications=_clean_choices(intended_applications),
            intended_models=(
                _clean_choices(intended_models)
                if intended_models is not None
                else workspace.intended_models
            ),
        )
        return self._save(
            updated,
            workspace,
            "WORKSPACE_TARGET_UPDATED",
            actor=actor,
        )

    def configure_transfer_destination(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        odoo_connection_mode: str,
        odoo_base_url: str,
        odoo_database: str,
    ) -> WorkspaceState:
        """Save a destination without replacing the Odoo source identity."""

        workspace = self._target_editable(
            workspace_id,
            expected_revision,
            actor=actor,
        )
        if workspace.source_mode is not SourceMode.ODOO:
            raise WorkspaceStateError(
                "A separate transfer destination is available only for Odoo source data"
            )
        if workspace.status is not WorkspaceStatus.REGISTERED:
            raise WorkspaceStateError(
                "Finish and register the Odoo source before connecting a destination"
            )
        try:
            connection_mode = OdooConnectionMode(odoo_connection_mode)
        except ValueError as error:
            raise WorkspaceStateError("Choose Local Odoo or Remote Odoo") from error
        try:
            base_url = validate_odoo_base_url(odoo_base_url, connection_mode)
        except ValueError as error:
            raise WorkspaceStateError(str(error)) from error
        database = _required_text(odoo_database, "Destination Odoo database")
        destination_hash = target_identity_hash(
            connection_mode=connection_mode.value,
            base_url=base_url,
            database=database,
        )
        source_hash = target_identity_hash(
            connection_mode=(
                workspace.odoo_connection_mode.value
                if workspace.odoo_connection_mode is not None
                else ""
            ),
            base_url=workspace.odoo_base_url,
            database=workspace.odoo_database,
        )
        candidate = replace(
            workspace,
            destination_odoo_connection_mode=connection_mode,
            destination_odoo_base_url=base_url,
            destination_odoo_database=database,
        )
        same_database = transfer_destination_matches_source(candidate)
        if destination_hash == source_hash or same_database:
            raise WorkspaceStateError(
                "Choose a different Odoo database for the destination"
            )
        changed = (
            workspace.destination_odoo_connection_mode is not connection_mode
            or workspace.destination_odoo_base_url != base_url
            or workspace.destination_odoo_database != database
        )
        updated = replace(
            workspace,
            destination_odoo_connection_mode=connection_mode,
            destination_odoo_base_url=base_url,
            destination_odoo_database=database,
            destination_verified_target_hash=(
                "" if changed else workspace.destination_verified_target_hash
            ),
            destination_verified_credential_binding_hash=(
                ""
                if changed
                else workspace.destination_verified_credential_binding_hash
            ),
            destination_verified_read_principal_hash=(
                "" if changed else workspace.destination_verified_read_principal_hash
            ),
            destination_verified_odoo_version=(
                "" if changed else workspace.destination_verified_odoo_version
            ),
            destination_verified_at=(
                None if changed else workspace.destination_verified_at
            ),
            destination_match_plan=(
                None if changed else workspace.destination_match_plan
            ),
            transfer_order_plan=(
                None if changed else workspace.transfer_order_plan
            ),
        )
        return self._save(
            updated,
            workspace,
            "WORKSPACE_TRANSFER_DESTINATION_UPDATED",
            actor=actor,
            detail=f"target={destination_hash}",
        )

    def verify_transfer_destination(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        target_hash: str,
        credential_binding_hash: str,
        read_principal_hash: str,
        odoo_version: str,
    ) -> WorkspaceState:
        """Bind successful read-only evidence to the configured destination."""

        workspace = self._target_editable(
            workspace_id,
            expected_revision,
            actor=actor,
        )
        expected_target_hash = transfer_destination_identity_hash(workspace)
        if not expected_target_hash or target_hash != expected_target_hash:
            raise WorkspaceStateError(
                "The checked Odoo destination no longer matches the saved destination"
            )
        if not odoo_version.startswith("19."):
            raise WorkspaceStateError("The destination must run Odoo 19")
        updated = replace(
            workspace,
            destination_verified_target_hash=target_hash,
            destination_verified_credential_binding_hash=_required_text(
                credential_binding_hash,
                "Destination credential binding",
            ),
            destination_verified_read_principal_hash=_required_text(
                read_principal_hash,
                "Destination read principal",
            ),
            destination_verified_odoo_version=odoo_version,
            destination_verified_at=_now(),
        )
        return self._save(
            updated,
            workspace,
            "WORKSPACE_TRANSFER_DESTINATION_VERIFIED",
            actor=actor,
            detail=f"target={target_hash}",
        )

    def save_destination_match_plan(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        plan: DestinationMatchPlan,
    ) -> WorkspaceState:
        """Publish current Stage 5 evidence after its bounded destination read."""

        workspace = self._target_editable(
            workspace_id,
            expected_revision,
            actor=actor,
        )
        if not workspace.destination_verified:
            raise WorkspaceStateError("Verify the destination Odoo connection first")
        if plan.workspace_id != workspace.workspace_id:
            raise WorkspaceStateError("Destination matching changed workspace identity")
        if (
            plan.destination_target_hash
            != transfer_destination_identity_hash(workspace)
            or plan.destination_credential_binding_hash
            != workspace.destination_verified_credential_binding_hash
            or plan.destination_read_principal_hash
            != workspace.destination_verified_read_principal_hash
        ):
            raise WorkspaceStateError(
                "Destination matching does not match the verified destination"
            )
        updated = replace(
            workspace,
            destination_match_plan=plan,
            transfer_order_plan=None,
        )
        return self._save(
            updated,
            workspace,
            "WORKSPACE_DESTINATION_MATCH_CHECKED",
            actor=actor,
            detail=(
                f"plan={plan.content_hash}; models={len(plan.model_matches)}; "
                f"ready={'yes' if plan.ready else 'no'}"
            ),
        )

    def save_transfer_order_plan(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        plan: TransferOrderPlan,
    ) -> WorkspaceState:
        """Publish Stage 6 ordering derived from current Stage 5 evidence."""

        workspace = self._target_editable(
            workspace_id,
            expected_revision,
            actor=actor,
        )
        match_plan = workspace.destination_match_plan
        if (
            match_plan is None
            or not workspace.destination_match_ready(
                source_selection_hash=plan.source_selection_hash,
                source_schema_hash=plan.source_schema_hash,
            )
            or plan.workspace_id != workspace.workspace_id
            or plan.destination_match_plan_hash != match_plan.content_hash
            or plan.source_selection_hash != match_plan.source_selection_hash
            or plan.source_schema_hash != match_plan.source_schema_hash
            or plan.destination_target_hash != match_plan.destination_target_hash
        ):
            raise WorkspaceStateError(
                "Transfer order does not match current destination matching"
            )
        updated = replace(workspace, transfer_order_plan=plan)
        return self._save(
            updated,
            workspace,
            "WORKSPACE_TRANSFER_ORDER_PLANNED",
            actor=actor,
            detail=(
                f"plan={plan.content_hash}; waves={len(plan.waves)}; "
                f"deferred={plan.deferred_dependency_count}; "
                f"ready={'yes' if plan.ready else 'no'}"
            ),
        )

    def _target_editable(
        self,
        workspace_id: str,
        expected_revision: int,
        *,
        actor: Actor,
    ) -> WorkspaceState:
        """Allow target setup after source registration.

        File-source workspaces intentionally defer their Odoo destination until the
        Odoo-data stage.  Concrete persistence already invalidates target-bound
        schema, mapping, and staging evidence when the identity changes.
        """

        _canonical_workspace_id(workspace_id)
        self.authorization.require(
            actor,
            Capability.PROJECT_EDIT,
            workspace_id=workspace_id,
        )
        workspace = self.repository.get(workspace_id)
        self.repository.assert_workspace_mutable(workspace.workspace_id)
        if workspace.revision != expected_revision:
            raise WorkspaceStateConflictError(
                "The workspace changed in another request; reload before continuing"
            )
        if workspace.status is WorkspaceStatus.CLOSED:
            raise WorkspaceStateError("Closed workspaces cannot be edited")
        return workspace

    def update_schema_scope(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        permitted_models: Sequence[str],
    ) -> WorkspaceState:
        """Set the exact Odoo models Stage C may read and map.

        This deliberately remains available after workspace registration. It is
        a schema-discovery decision, rather than a change to the registered
        Odoo target or the workspace's business context.
        """

        _canonical_workspace_id(workspace_id)
        self.authorization.require(
            actor,
            Capability.SCHEMA_DISCOVER,
            workspace_id=workspace_id,
        )
        workspace = self.repository.get(workspace_id)
        self.repository.assert_workspace_mutable(workspace.workspace_id)
        if workspace.revision != expected_revision:
            raise WorkspaceStateConflictError(
                "The workspace changed in another request; reload before continuing"
            )
        if workspace.status is not WorkspaceStatus.REGISTERED:
            raise WorkspaceStateError(
                "Register the workspace before setting its permitted model scope"
            )
        models = _clean_choices(permitted_models)
        if not models:
            raise WorkspaceStateError("Add at least one permitted technical Odoo model")
        if models == workspace.intended_models:
            return workspace
        updated = replace(
            workspace,
            intended_models=models,
            mapping_version=None,
            approval_status=(
                ApprovalStatus.INVALIDATED
                if workspace.mapping_version
                else workspace.approval_status
            ),
        )
        saved = replace(
            updated,
            revision=workspace.revision + 1,
            updated_at=_now(),
        )
        self.repository.update_schema_scope(
            saved,
            expected_revision=workspace.revision,
            actor=actor,
        )
        return saved

    def record_credential_event(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        role: str,
        action: str,
        binding_hash: str,
        persistent: bool,
    ) -> None:
        """Audit a successful vault mutation without recording its secret."""

        canonical_workspace_id = _canonical_workspace_id(workspace_id)
        normalized_role = role.strip().upper()
        normalized_action = action.strip().upper()
        if normalized_role not in {"READ", "WRITE", "DESTINATION_READ"}:
            raise WorkspaceStateError("Credential audit role is invalid")
        if normalized_action not in {"STORED", "REPLACED"}:
            raise WorkspaceStateError("Credential audit action is invalid")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", binding_hash) is None:
            raise WorkspaceStateError("Credential audit binding is invalid")
        self.authorization.require(
            actor,
            (
                Capability.SCHEMA_DISCOVER
                if normalized_role in {"READ", "DESTINATION_READ"}
                else Capability.EXPORT_PLAN_EXECUTE
            ),
            workspace_id=canonical_workspace_id,
        )
        self.repository.record_credential_event(
            canonical_workspace_id,
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
        workspace_id: str,
        role: str,
        reason: str,
        connection_target_hash: str,
        credential_binding_hash: str | None,
        storage_class: str,
        removed_at: datetime,
        actor: Actor,
    ) -> None:
        """Retain one actor-bound receipt after a vault entry is removed."""

        canonical_workspace_id = _canonical_workspace_id(workspace_id)
        normalized_role = role.strip().upper()
        normalized_reason = reason.strip().upper()
        normalized_storage = storage_class.strip().upper()
        if normalized_role not in {"READ", "WRITE", "DESTINATION_READ"}:
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
            workspace_id=canonical_workspace_id,
        )
        self.repository.record_credential_removal_receipt(
            receipt_hash=receipt_hash,
            workspace_id=canonical_workspace_id,
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
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
        source_file: SourceFile,
    ) -> WorkspaceState:
        """Attach one source file before the workspace's tables are frozen."""

        workspace = self._source_files_editable(
            workspace_id,
            expected_revision,
            actor=actor,
        )
        if workspace.source_mode is not SourceMode.FILE:
            raise WorkspaceStateError("Odoo-source projects do not accept source files")
        if any(item.sha256 == source_file.sha256 for item in workspace.source_files):
            raise WorkspaceStateError("This exact source file is already registered")
        updated = replace(workspace, source_files=workspace.source_files + (source_file,))
        saved = replace(
            updated,
            revision=workspace.revision + 1,
            updated_at=_now(),
        )
        self.repository.add_source_file(
            saved,
            source_file,
            expected_revision=workspace.revision,
            actor=actor,
        )
        return saved

    def remove_source_file(
        self,
        workspace_id: str,
        file_id: str,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> SourceFile:
        """Remove one source file before any table selection has been frozen."""

        workspace = self._source_files_editable(
            workspace_id,
            expected_revision,
            actor=actor,
        )
        if workspace.source_mode is not SourceMode.FILE:
            raise WorkspaceStateError("Odoo-source projects do not contain source files")
        source_file = next(
            (item for item in workspace.source_files if item.file_id == file_id),
            None,
        )
        if source_file is None:
            raise WorkspaceStateError("The selected source file is no longer in this workspace")
        saved = replace(
            workspace,
            source_files=tuple(
                item for item in workspace.source_files if item.file_id != file_id
            ),
            revision=workspace.revision + 1,
            updated_at=_now(),
        )
        self.repository.remove_source_file(
            saved,
            source_file,
            expected_revision=workspace.revision,
            actor=actor,
        )
        return source_file

    def _source_files_editable(
        self,
        workspace_id: str,
        expected_revision: int,
        *,
        actor: Actor,
    ) -> WorkspaceState:
        """Allow file-list amendments in draft or before registered table freeze."""

        _canonical_workspace_id(workspace_id)
        self.authorization.require(
            actor,
            Capability.PROJECT_EDIT,
            workspace_id=workspace_id,
        )
        workspace = self.repository.get(workspace_id)
        self.repository.assert_workspace_mutable(workspace.workspace_id)
        if workspace.revision != expected_revision:
            raise WorkspaceStateConflictError(
                "The workspace changed in another request; reload before continuing"
            )
        if workspace.status is WorkspaceStatus.CLOSED:
            raise WorkspaceStateError("Closed workspaces cannot be edited")
        return workspace

    def register(
        self,
        workspace_id: str,
        *,
        actor: Actor,
        expected_revision: int,
    ) -> WorkspaceState:
        """Register a complete draft and close the editable setup boundary.

        All problems from :func:`workspace_registration_problems` are returned together
        through ``WorkspaceRegistrationError``. Registration is workspace evidence,
        not mapping, normalization, package, or execution approval.
        """

        workspace = self._editable(
            workspace_id,
            expected_revision,
            actor=actor,
            capability=Capability.PROJECT_REGISTER,
        )
        problems = workspace_registration_problems(workspace)
        if problems:
            raise WorkspaceRegistrationError(problems)
        registered = replace(
            workspace,
            status=WorkspaceStatus.REGISTERED,
            registered_at=_now(),
        )
        return self._save(
            registered,
            workspace,
            "WORKSPACE_REGISTERED",
            actor=actor,
        )

    def _editable(
        self,
        workspace_id: str,
        expected_revision: int,
        *,
        actor: Actor,
        capability: Capability,
    ) -> WorkspaceState:
        _canonical_workspace_id(workspace_id)
        self.authorization.require(
            actor,
            capability,
            workspace_id=workspace_id,
        )
        workspace = self.repository.get(workspace_id)
        self.repository.assert_workspace_mutable(workspace.workspace_id)
        if workspace.revision != expected_revision:
            raise WorkspaceStateConflictError(
                "The workspace changed in another request; reload before continuing"
            )
        if workspace.status is not WorkspaceStatus.DRAFT:
            raise WorkspaceStateError("Registered or closed workspaces cannot be edited")
        return workspace

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


def transfer_destination_identity_hash(workspace: WorkspaceState) -> str:
    """Return the exact configured destination identity, or an empty value."""

    if not workspace.destination_configured:
        return ""
    assert workspace.destination_odoo_connection_mode is not None
    return target_identity_hash(
        connection_mode=workspace.destination_odoo_connection_mode.value,
        base_url=workspace.destination_odoo_base_url,
        database=workspace.destination_odoo_database,
    )


def transfer_destination_matches_source(workspace: WorkspaceState) -> bool:
    """Whether source and destination identify the same Odoo database."""

    return bool(
        workspace.destination_configured
        and workspace.odoo_base_url
        and workspace.odoo_database
        and workspace.destination_odoo_base_url.rstrip("/").casefold()
        == workspace.odoo_base_url.rstrip("/").casefold()
        and workspace.destination_odoo_database.casefold()
        == workspace.odoo_database.casefold()
    )


def transfer_destination_workspace(workspace: WorkspaceState) -> WorkspaceState:
    """Project the destination through existing bounded Odoo read ports."""

    if not workspace.destination_configured:
        raise WorkspaceStateError("Configure the Odoo destination first")
    return replace(
        workspace,
        odoo_connection_mode=workspace.destination_odoo_connection_mode,
        odoo_base_url=workspace.destination_odoo_base_url,
        odoo_database=workspace.destination_odoo_database,
        intended_applications=(),
        intended_models=(),
    )


def workspace_setup_requirements(
    workspace: WorkspaceState,
) -> tuple[WorkspaceSetupRequirement, ...]:
    """Return every unmet setup requirement with one owning browser step."""

    requirements: list[WorkspaceSetupRequirement] = []
    if workspace.source_mode is SourceMode.FILE:
        if not workspace.source_files:
            requirements.append(
                WorkspaceSetupRequirement(
                    code="SOURCE_FILE_REQUIRED",
                    step=WorkspaceSetupStep.FILES,
                    problem="At least one source file is required",
                    guidance="Add at least one source file.",
                )
            )
    if (
        workspace.source_mode is SourceMode.ODOO
        and workspace.odoo_connection_mode is None
    ):
        requirements.append(
            WorkspaceSetupRequirement(
                code="ODOO_CONNECTION_MODE_REQUIRED",
                step=WorkspaceSetupStep.TARGET,
                problem="Choose a Local Odoo or Remote Odoo connection",
                guidance="Choose where Odoo is running.",
            )
        )
    if workspace.source_mode is SourceMode.ODOO and not workspace.odoo_base_url:
        requirements.append(
            WorkspaceSetupRequirement(
                code="ODOO_BASE_URL_REQUIRED",
                step=WorkspaceSetupStep.TARGET,
                problem="Odoo base URL is required",
                guidance="Enter the Odoo web address.",
            )
        )
    if workspace.source_mode is SourceMode.ODOO and not workspace.odoo_database:
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
    workspace: WorkspaceState,
    step: WorkspaceSetupStep,
) -> tuple[WorkspaceSetupRequirement, ...]:
    """Return only the unmet requirements owned by ``step``."""

    return tuple(
        requirement
        for requirement in workspace_setup_requirements(workspace)
        if requirement.step is step
    )


def workspace_registration_problems(workspace: WorkspaceState) -> tuple[str, ...]:
    """Return every user-actionable reason a draft cannot be registered."""

    return tuple(
        requirement.problem
        for requirement in workspace_setup_requirements(workspace)
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


def _clean_choices(values: Sequence[str]) -> tuple[str, ...]:
    cleaned = {value.strip() for value in values if value.strip()}
    return tuple(sorted(cleaned, key=str.casefold))


def _canonical_workspace_id(workspace_id: str) -> str:
    try:
        return str(UUID(workspace_id))
    except (ValueError, AttributeError) as error:
        raise WorkspaceStateNotFoundError("Invalid workspace identifier") from error
