"""Describe durable browser mutation receipts for Match data authoring.

Layer: domain contract. A browser operation identity is reserved before a
mapping command starts. The repository then records ``COMMITTED`` in the same
transaction as the authoritative mapping write, or records ``REJECTED`` after
a command is rejected. A surviving ``PENDING`` receipt is intentionally an
unknown outcome rather than permission to repeat the mutation blindly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from impodo.domain.workspace.errors import WorkspaceError


class MappingMutationAction(StrEnum):
    """Closed browser command vocabulary for the Match data editor."""

    SAVE_PROGRESS = "SAVE_PROGRESS"
    CHECK_MATCHES = "CHECK_MATCHES"
    CONFIRM_MATCHES = "CONFIRM_MATCHES"
    REMOVE_READONLY = "REMOVE_READONLY"
    CONFIRM_DEFAULTS = "CONFIRM_DEFAULTS"
    REFRESH_DEFAULTS = "REFRESH_DEFAULTS"
    SET_DISPOSITION = "SET_DISPOSITION"
    CLEAR_DISPOSITION = "CLEAR_DISPOSITION"


class MappingMutationState(StrEnum):
    """Terminal or deliberately non-terminal receipt state."""

    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class MappingMutationReceipt:
    """Durable evidence for one browser-authored mapping command."""

    operation_id: str
    workspace_id: str
    action: MappingMutationAction
    request_hash: str
    state: MappingMutationState
    submitted_working_draft_version: int | None
    submitted_mapping_revision_version: int | None
    working_draft_version: int | None
    mapping_revision_version: int | None
    content_identity: str
    failure_code: str
    failure_detail: str
    started_at: datetime
    completed_at: datetime | None
    actor_issuer: str
    actor_subject: str
    replayed: bool = False

    def __post_init__(self) -> None:
        try:
            UUID(self.operation_id)
        except (TypeError, ValueError, AttributeError) as error:
            raise WorkspaceError("Mapping operation identity must be a UUID") from error
        object.__setattr__(self, "action", MappingMutationAction(self.action))
        object.__setattr__(self, "state", MappingMutationState(self.state))
        if len(self.request_hash) != 64:
            raise WorkspaceError("Mapping operation request hash is invalid")
        if self.content_identity and not (
            len(self.content_identity) == 71
            and self.content_identity.startswith("sha256:")
        ):
            raise WorkspaceError("Mapping operation content identity is invalid")
        if self.state is MappingMutationState.PENDING and self.completed_at is not None:
            raise WorkspaceError("Pending mapping operation cannot be completed")
        if self.state is not MappingMutationState.PENDING and self.completed_at is None:
            raise WorkspaceError("Terminal mapping operation needs a completion time")

    def portable_dict(self) -> dict[str, object]:
        """Return the bounded receipt shape exposed to the local browser."""

        return {
            "operation_id": self.operation_id,
            "action": self.action.value,
            "status": self.state.value.lower(),
            "submitted_working_draft_version": (
                self.submitted_working_draft_version
            ),
            "submitted_mapping_revision_version": (
                self.submitted_mapping_revision_version
            ),
            "expected_working_draft_version": self.working_draft_version,
            "expected_parent_version": self.mapping_revision_version,
            "content_identity": self.content_identity,
            "failure_code": self.failure_code,
            "detail": self.failure_detail,
            "started_at": self.started_at.isoformat(),
            "completed_at": (
                self.completed_at.isoformat()
                if self.completed_at is not None
                else None
            ),
        }


class MappingVersionConflict(WorkspaceError):
    """Reject one stale editor command without discarding submitted choices."""

    code = "MAPPING_VERSION_CONFLICT"

    def __init__(
        self,
        *,
        submitted_working_draft_version: int | None,
        submitted_mapping_revision_version: int | None,
        current_working_draft_version: int | None,
        current_mapping_revision_version: int | None,
    ) -> None:
        self.submitted_working_draft_version = submitted_working_draft_version
        self.submitted_mapping_revision_version = (
            submitted_mapping_revision_version
        )
        self.current_working_draft_version = current_working_draft_version
        self.current_mapping_revision_version = current_mapping_revision_version
        super().__init__(
            "This page is out of date because newer Match data was saved. "
            "Your edits are still on this page."
        )
