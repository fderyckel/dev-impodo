"""Expose bounded, value-free field counts from existing impact evidence.

Local composition reads the worker-owned snapshot. A server composition
injects a verified worker reader; no row values or mapping payloads cross
this port, and reviewing fields never evaluates rules.
"""

from dataclasses import dataclass
from typing import Protocol

from impodo.domain.shared.access import Actor, Capability, WorkspaceAuthorizationPolicy
from impodo.domain.staging.transformation_impact import (
    TransformationFieldImpact,
    TransformationImpactIdentity,
)
from impodo.domain.workspace.errors import WorkspaceError

FIELD_EFFECTS_LIMIT = 100


@dataclass(frozen=True, slots=True)
class FieldEffectsProjection:
    """Describe current or historical counts without representative values."""

    available: bool = False
    current: bool = False
    fields: tuple[TransformationFieldImpact, ...] = ()


class FieldEffectsRepository(Protocol):
    """Read only the requested field metadata from a committed snapshot."""

    def get_transformation_field_effects(
        self,
        workspace_id: str,
        identity: TransformationImpactIdentity | None,
        fields: tuple[tuple[str, str], ...],
    ) -> FieldEffectsProjection: ...


class FieldEffectsQuery(Protocol):
    """Replace with a verified bounded worker projection on a server."""

    def review(
        self,
        workspace_id: str,
        identity: TransformationImpactIdentity | None,
        fields: tuple[tuple[str, str], ...],
        *,
        actor: Actor,
    ) -> FieldEffectsProjection: ...


class FieldEffectsService:
    """Authorize the bulk field query before any local evidence access."""

    def __init__(
        self,
        repository: FieldEffectsRepository,
        authorization: WorkspaceAuthorizationPolicy,
    ):
        self.repository, self.authorization = repository, authorization

    def review(
        self,
        workspace_id: str,
        identity: TransformationImpactIdentity | None,
        fields: tuple[tuple[str, str], ...],
        *,
        actor: Actor,
    ) -> FieldEffectsProjection:
        """Return at most one catalog page, without capture or evaluation."""
        self.authorization.require(
            actor, Capability.MAPPING_EDIT, workspace_id=workspace_id
        )
        if len(fields) > FIELD_EFFECTS_LIMIT or len(set(fields)) != len(fields):
            raise WorkspaceError(
                "Choose one bounded page of distinct rule-effect fields"
            )
        if not fields:
            return FieldEffectsProjection()
        return self.repository.get_transformation_field_effects(
            workspace_id, identity, fields
        )
