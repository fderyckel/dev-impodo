"""Review supporting choices using injected evidence and persistence ports.

Local operations gather metadata, then release their database owners before
the web composition performs any Odoo read. Server adapters keep the same
contract without granting a browser access to worker DuckDB.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from impodo.application.workspace.mapping.supporting_values import (
    MappingSupportingRelationship,
    MappingSupportingRequirement,
)
from impodo.domain.schema.governance import SchemaGovernance
from impodo.domain.serialization import content_hash
from impodo.domain.shared.access import Actor, Capability, WorkspaceAuthorizationPolicy
from impodo.domain.shared.models import target_identity_hash
from impodo.domain.workspace.contracts import (
    OdooSchemaCatalog,
    SchemaOrigin,
    SourceSelection,
)
from impodo.domain.workspace.derived_entities import DerivedEntityPlan
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.reference_keys import reference_policy_hash
from impodo.domain.workspace.supporting_models import (
    SupportingModelRole,
    supporting_model_role,
)
from impodo.domain.workspace.supporting_plan import SupportingSourceDescriptor
from impodo.domain.workspace.supporting_review import (
    SupportingChoice,
    SupportingChoiceAction,
    SupportingReview,
    SupportingReviewSummary,
    eligible_supporting_fields,
    supporting_read_display_field,
    supporting_read_request,
)
from impodo.domain.workspace.supporting_sources import supporting_source_descriptors
from impodo.domain.workspace.workbench import SourceMode, WorkspaceState


class SupportingReviewRepository(Protocol):
    """Read one current revision or append one optimistic, idempotent revision."""

    def get_supporting_review(self, workspace_id: str) -> SupportingReview | None: ...

    def get_supporting_review_summary(
        self, workspace_id: str
    ) -> SupportingReviewSummary | None: ...

    def rebuild_supporting_review_summary(
        self, workspace_id: str
    ) -> SupportingReviewSummary | None: ...

    def save_supporting_review(
        self,
        workspace_id: str,
        review: SupportingReview,
        *,
        expected_version: int,
        actor: Actor,
    ) -> SupportingReview: ...


@dataclass(frozen=True, slots=True)
class SupportingReviewPacket:
    """One closed set of metadata, never an open database or remote reader."""

    workspace: WorkspaceState
    selection: SourceSelection
    schema: OdooSchemaCatalog
    preparation: DerivedEntityPlan | None
    governance: SchemaGovernance | None
    descriptors: tuple[SupportingSourceDescriptor, ...]
    binding_hash: str


def supporting_review_packet(
    workspace: WorkspaceState,
    selection: SourceSelection,
    schema: OdooSchemaCatalog,
    preparation: DerivedEntityPlan | None,
    governance: SchemaGovernance | None,
) -> SupportingReviewPacket:
    """Reuse page evidence instead of rereading Workspace or source packages."""
    if (
        workspace.source_mode is not SourceMode.FILE
        or schema.origin is not SchemaOrigin.LIVE_API
    ):
        raise WorkspaceError(
            "Supporting review requires accepted file data and live Odoo details"
        )
    if preparation is not None and preparation.workspace_id != workspace.workspace_id:
        raise WorkspaceError("Supporting preparation belongs to another workspace")
    descriptors = supporting_source_descriptors(selection, preparation)
    binding = content_hash(
        {
            "workspace": workspace.workspace_id,
            "source": selection.content_hash,
            "preparation": preparation.content_hash if preparation else None,
            "schema": schema.content_hash,
            "scope": sorted(workspace.intended_models),
            "target": schema.connection_target_hash,
            "workspace_target": target_identity_hash(
                connection_mode=workspace.odoo_connection_mode.value
                if workspace.odoo_connection_mode
                else "",
                base_url=workspace.odoo_base_url,
                database=workspace.odoo_database,
            ),
            "version": schema.odoo_version,
            "credential": schema.read_credential_binding_hash,
            "principal": schema.read_principal_hash,
            "permission": schema.read_permission_hash,
            "context": schema.read_context_hash,
            "policy": tuple(reference_policy_hash(major) for major in (19, 20)),
        }
    )
    return SupportingReviewPacket(
        workspace, selection, schema, preparation, governance, descriptors, binding
    )


class SupportingReviewService:
    """Validate business choices; writes and value matching keep their owners."""

    def __init__(
        self,
        repository: SupportingReviewRepository,
        authorization: WorkspaceAuthorizationPolicy,
    ) -> None:
        self.repository = repository
        self.authorization = authorization

    def current(self, workspace_id: str, *, actor: Actor) -> SupportingReview | None:
        """Authorize before the single bounded revision read."""
        self.authorization.require(
            actor, Capability.PROJECT_VIEW, workspace_id=workspace_id
        )
        return self.repository.get_supporting_review(workspace_id)

    def summary(
        self, workspace_id: str, *, actor: Actor
    ) -> SupportingReviewSummary | None:
        """Read only bounded committed counts and evidence pointers."""
        self.authorization.require(
            actor, Capability.PROJECT_VIEW, workspace_id=workspace_id
        )
        return self.repository.get_supporting_review_summary(workspace_id)

    def rebuild_summary(
        self, workspace_id: str, *, actor: Actor
    ) -> SupportingReviewSummary | None:
        """Rebuild local projection metadata without recapturing Odoo values."""
        self.authorization.require(
            actor, Capability.SCHEMA_GOVERN, workspace_id=workspace_id
        )
        return self.repository.rebuild_supporting_review_summary(workspace_id)

    @staticmethod
    def normalize(
        packet: SupportingReviewPacket, choices: tuple[SupportingChoice, ...]
    ) -> tuple[SupportingChoice, ...]:
        """Prove every saved edge against current captured requirements in memory."""
        fields = eligible_supporting_fields(packet.schema)
        descriptors = {item.dataset_id: item for item in packet.descriptors}
        result = []
        seen = set()
        if len(choices) > 1_000:
            raise WorkspaceError("Review at most 1,000 supporting relationships")
        for choice in choices:
            identity = (choice.source_model, choice.field_name)
            field = fields.get(identity)
            if field is None or identity in seen:
                raise WorkspaceError(
                    "The supporting relationship changed or was submitted twice"
                )
            seen.add(identity)
            role = supporting_model_role(field)
            action = choice.action
            if action is SupportingChoiceAction.SKIP_OPTIONAL and field.required:
                raise WorkspaceError(
                    "A required supporting relationship cannot be left empty"
                )
            if (
                role is SupportingModelRole.ODOO_MANAGED
                and action is not SupportingChoiceAction.ODOO_MANAGED
            ):
                raise WorkspaceError("This supporting relationship is managed by Odoo")
            if (
                action is SupportingChoiceAction.ODOO_MANAGED
                and role is not SupportingModelRole.ODOO_MANAGED
            ):
                raise WorkspaceError(
                    "Odoo does not manage this supporting relationship"
                )
            if (
                action is SupportingChoiceAction.CHECKED_DEFAULT
                and not field.create_default_present
            ):
                raise WorkspaceError(
                    "Check an available Odoo default before choosing it"
                )
            if action is SupportingChoiceAction.INCOMING:
                candidate = descriptors.get(choice.dataset_id)
                if candidate is None:
                    named = tuple(
                        item
                        for item in packet.descriptors
                        if item.label == choice.dataset_id
                    )
                    candidate = named[0] if len(named) == 1 else None
                if candidate is None or (
                    candidate.target_model and candidate.target_model != field.relation
                ):
                    raise WorkspaceError("Choose a compatible accepted incoming table")
                if field.relation not in packet.workspace.intended_models:
                    raise WorkspaceError(
                        "Explicitly add the related business records to the write scope first"
                    )
                choice = replace(choice, dataset_id=candidate.dataset_id)
            if action is SupportingChoiceAction.REUSE:
                try:
                    request = supporting_read_request(
                        packet.schema, packet.governance, choice
                    )
                except ValueError as error:
                    raise WorkspaceError(str(error)) from error
                display = supporting_read_display_field(packet.schema, request)
                expected = (request.key_fields, request.scope_fields, display)
                if (
                    choice.key_fields or choice.scope_fields or choice.display_field
                ) and (
                    choice.key_fields,
                    choice.scope_fields,
                    choice.display_field,
                ) != expected:
                    raise WorkspaceError(
                        "The exact supporting read scope changed; review this choice again"
                    )
                choice = replace(
                    choice,
                    key_fields=request.key_fields,
                    scope_fields=request.scope_fields,
                    display_field=display,
                )
            result.append(choice)
        missing = [
            field.label
            for key, field in fields.items()
            if field.required and key not in seen
        ]
        if missing:
            raise WorkspaceError(
                "Review required supporting data: " + ", ".join(sorted(missing))
            )
        return tuple(sorted(result))

    def save(
        self,
        packet: SupportingReviewPacket,
        *,
        expected_binding_hash: str,
        expected_version: int,
        operation_id: str,
        choices: tuple[SupportingChoice, ...],
        actor: Actor,
    ) -> SupportingReview:
        """Persist one exact current revision; a retry cannot widen its meaning."""
        self.authorization.require(
            actor, Capability.SCHEMA_GOVERN, workspace_id=packet.workspace.workspace_id
        )
        if packet.binding_hash != expected_binding_hash:
            raise WorkspaceError(
                "Supporting data changed; review the current choices again"
            )
        operation_id = str(UUID(operation_id))
        normalized = self.normalize(packet, choices)
        review = SupportingReview(
            packet.workspace.workspace_id,
            expected_version + 1,
            operation_id,
            packet.binding_hash,
            normalized,
            datetime.now(UTC).isoformat(),
            actor.identity.issuer,
            actor.identity.subject_id,
            actor.identity.display_name,
        )
        return self.repository.save_supporting_review(
            packet.workspace.workspace_id,
            review,
            expected_version=expected_version,
            actor=actor,
        )

    def require_current(
        self,
        packet: SupportingReviewPacket,
        *,
        actor: Actor,
        review: SupportingReview | None = None,
        read_if_missing: bool = True,
    ) -> SupportingReview | None:
        """Use the same currentness and completeness gate for pages and commands."""
        self.authorization.require(
            actor, Capability.PROJECT_VIEW, workspace_id=packet.workspace.workspace_id
        )
        if review is None and read_if_missing:
            review = self.current(packet.workspace.workspace_id, actor=actor)
        return self.validate_review(packet, review)

    @staticmethod
    def validate_review(
        packet: SupportingReviewPacket, review: SupportingReview | None
    ) -> SupportingReview | None:
        """Verify supplied evidence after its owning caller has authorized access."""
        fields = eligible_supporting_fields(packet.schema)
        if review is None:
            if any(item.required for item in fields.values()):
                raise WorkspaceError(
                    "Review the required supporting-data choices before confirming matching rules"
                )
            return None
        if (
            review.workspace_id != packet.workspace.workspace_id
            or review.binding_hash != packet.binding_hash
        ):
            raise WorkspaceError(
                "Supporting-data choices need refresh; review and save them again"
            )
        if SupportingReviewService.normalize(packet, review.choices) != review.choices:
            raise WorkspaceError(
                "Supporting matching keys changed; review the choices again"
            )
        return review

    def requirements(
        self, packet: SupportingReviewPacket, review: SupportingReview, *, actor: Actor
    ) -> tuple[MappingSupportingRequirement, ...]:
        """Group exact reviewed reuse reads without promoting related models."""
        self.authorization.require(
            actor,
            Capability.SCHEMA_DISCOVER,
            workspace_id=packet.workspace.workspace_id,
        )
        self.require_current(packet, actor=actor, review=review)
        grouped: dict[
            tuple[str, tuple[str, ...], tuple[str, ...]],
            list[MappingSupportingRelationship],
        ] = {}
        for choice in review.choices:
            if choice.action is not SupportingChoiceAction.REUSE:
                continue
            request = supporting_read_request(packet.schema, packet.governance, choice)
            grouped.setdefault(
                (request.related_model, request.key_fields, request.scope_fields), []
            ).append(
                MappingSupportingRelationship(
                    request.parent_model,
                    request.relationship_field,
                    request.relationship_type,
                )
            )
        return tuple(
            MappingSupportingRequirement(
                model, key, scope, tuple(grouped[(model, key, scope)]), ()
            )
            for model, key, scope in sorted(grouped)
        )
