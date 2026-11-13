"""Declare authorized protected storage for checked and reusable identity aliases."""

from __future__ import annotations

from typing import Protocol

from impodo.domain.workspace.destination_matching import DestinationMatchPlan
from impodo.domain.workspace.transfer_identity_aliases import (
    DestinationIdentityAliasEvidence,
    DestinationIdentityAliasRule,
    ProtectedIdentityAliasRulesReference,
)


class DestinationIdentityAliasStore(Protocol):
    """Require callers to authorize project and protected-evidence access first."""

    def put(
        self, project_id: str, plan: DestinationMatchPlan
    ) -> DestinationMatchPlan: ...

    def read(
        self, project_id: str, plan: DestinationMatchPlan
    ) -> DestinationIdentityAliasEvidence | None: ...

    def put_rules(
        self,
        project_id: str,
        *,
        owner_key: str,
        values: tuple[DestinationIdentityAliasRule, ...],
    ) -> ProtectedIdentityAliasRulesReference: ...

    def read_rules(
        self,
        project_id: str,
        reference: ProtectedIdentityAliasRulesReference,
        *,
        owner_key: str,
    ) -> tuple[DestinationIdentityAliasRule, ...]: ...
