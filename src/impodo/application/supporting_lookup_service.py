"""Authorize capture and safe reuse of Many2one supporting lookups."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from impodo.domain.shared.access import Actor, AuthorizationPolicy, Capability
from impodo.domain.workspace.supporting_lookups import (
    SupportingLookupChoice,
    SupportingLookupSnapshot,
    supporting_lookup_key,
)
from impodo.domain.workspace.reference_keys import (
    REFERENCE_POLICY_HASH,
    StandardReferenceFieldContract,
)


class SupportingLookupRepositoryPort(Protocol):
    """Persistence required by the supporting-lookup use case."""

    def list_current_for_model(
        self,
        workspace_id: str,
        relation_model: str,
    ) -> tuple[SupportingLookupSnapshot, ...]: ...

    def get_current(
        self,
        workspace_id: str,
        lookup_key: str,
    ) -> SupportingLookupSnapshot | None: ...

    def save(
        self,
        workspace_id: str,
        snapshot: SupportingLookupSnapshot,
        *,
        actor: Actor,
    ) -> None: ...


class SupportingLookupService:
    """Keep incidental lookup data out of the primary schema lifecycle."""

    def __init__(
        self,
        repository: SupportingLookupRepositoryPort,
        authorization: AuthorizationPolicy,
    ) -> None:
        self._repository = repository
        self._authorization = authorization

    def current(
        self,
        workspace_id: str,
        *,
        relation_model: str,
        key_fields: tuple[str, ...],
        scope_fields: tuple[str, ...],
        display_field: str,
        target_hash: str,
        read_credential_binding_hash: str,
        read_principal_hash: str,
        read_context_hash: str,
        actor: Actor,
        reference_policy_hash: str = REFERENCE_POLICY_HASH,
    ) -> SupportingLookupSnapshot | None:
        """Return current choices only when target and read context still match."""

        self._authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        lookup_key = supporting_lookup_key(
            relation_model=relation_model,
            key_fields=key_fields,
            scope_fields=scope_fields,
            display_field=display_field,
            reference_policy_hash=reference_policy_hash,
        )
        snapshot = self._repository.get_current(workspace_id, lookup_key)
        if snapshot is None:
            return None
        expected = (
            workspace_id,
            target_hash,
            read_credential_binding_hash,
            read_principal_hash,
            read_context_hash,
        )
        actual = (
            snapshot.workspace_id,
            snapshot.target_hash,
            snapshot.read_credential_binding_hash,
            snapshot.read_principal_hash,
            snapshot.read_context_hash,
        )
        return (
            snapshot
            if actual == expected
            and snapshot.reference_policy_hash == reference_policy_hash
            else None
        )

    def current_preflight_reference(
        self,
        workspace_id: str,
        *,
        relation_model: str,
        key_fields: tuple[str, ...],
        scope_fields: tuple[str, ...],
        target_hash: str,
        read_credential_binding_hash: str,
        read_principal_hash: str,
        read_context_hash: str,
        actor: Actor,
        reference_policy_hash: str = REFERENCE_POLICY_HASH,
    ) -> SupportingLookupSnapshot | None:
        """Resolve exact target-bound evidence for a preflight reference.

        Display fields are deliberately not part of the lookup. Final review
        authorizes only the key/scope fields present in its bounded request,
        while Match data may have captured different display projections for
        the same portable reference identity.
        """

        self._authorization.require(
            actor,
            Capability.PREFLIGHT_RUN,
            workspace_id=workspace_id,
        )
        expected = (
            workspace_id,
            relation_model,
            key_fields,
            scope_fields,
            target_hash,
            read_credential_binding_hash,
            read_principal_hash,
            read_context_hash,
            reference_policy_hash,
        )
        candidates = []
        for snapshot in self._repository.list_current_for_model(
            workspace_id,
            relation_model,
        ):
            actual = (
                snapshot.workspace_id,
                snapshot.relation_model,
                snapshot.key_fields,
                snapshot.scope_fields,
                snapshot.target_hash,
                snapshot.read_credential_binding_hash,
                snapshot.read_principal_hash,
                snapshot.read_context_hash,
                snapshot.reference_policy_hash,
            )
            if actual == expected:
                candidates.append(snapshot)
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item.captured_at, item.snapshot_id))

    def capture(
        self,
        workspace_id: str,
        *,
        relation_model: str,
        key_fields: tuple[str, ...],
        scope_fields: tuple[str, ...],
        display_field: str,
        field_contracts: tuple[StandardReferenceFieldContract, ...],
        target_hash: str,
        read_credential_binding_hash: str,
        read_principal_hash: str,
        read_permission_hash: str,
        read_context_hash: str,
        captured_at: datetime,
        choices: tuple[SupportingLookupChoice, ...],
        ambiguous_values: tuple[str, ...],
        actor: Actor,
        reference_policy_hash: str = REFERENCE_POLICY_HASH,
    ) -> SupportingLookupSnapshot:
        """Persist one immutable lookup revision and make it current."""

        self._authorization.require(
            actor,
            Capability.MAPPING_EDIT,
            workspace_id=workspace_id,
        )
        snapshot = SupportingLookupSnapshot.capture(
            workspace_id=workspace_id,
            relation_model=relation_model,
            key_fields=key_fields,
            scope_fields=scope_fields,
            display_field=display_field,
            field_contracts=field_contracts,
            target_hash=target_hash,
            read_credential_binding_hash=read_credential_binding_hash,
            read_principal_hash=read_principal_hash,
            read_permission_hash=read_permission_hash,
            read_context_hash=read_context_hash,
            captured_at=captured_at,
            captured_by=(
                f"{actor.identity.issuer}:{actor.identity.subject_id}"
            ),
            choices=choices,
            ambiguous_values=ambiguous_values,
            reference_policy_hash=reference_policy_hash,
        )
        self._repository.save(workspace_id, snapshot, actor=actor)
        return snapshot
