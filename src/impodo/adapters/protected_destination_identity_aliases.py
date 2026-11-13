"""Encrypt reviewed reference aliases separately from required create defaults.

The caller owns authorization before every operation. Project evidence binds
encryption and integrity to the project; this adapter supplies exact semantic
bindings and never makes a retained choice authorize a future target write.
"""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.workspace.destination_matching import (
    DestinationMatchPlan,
    validate_destination_identity_alias_evidence,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.transfer_identity_aliases import (
    DestinationIdentityAliasEvidence,
    DestinationIdentityAliasRule,
    ProtectedIdentityAliasRulesReference,
)

from .protected_project_evidence_store import ProtectedProjectEvidenceStore

_KIND = "destination-identity-aliases"
_RULES_KIND = "destination-identity-alias-rules"


class ProtectedDestinationIdentityAliasStore:
    """Store immutable per-read evidence and separately owned reusable rules."""

    def __init__(self, evidence: ProtectedProjectEvidenceStore) -> None:
        self._evidence = evidence

    def put(self, project_id: str, plan: DestinationMatchPlan) -> DestinationMatchPlan:
        protected = plan.identity_alias_evidence
        if protected is None:
            if (
                plan.identity_alias_bindings
                and plan.protected_identity_alias_artifact_hash is None
            ):
                raise WorkspaceError(
                    "Protected destination identity aliases are missing; check matching again"
                )
            return plan
        try:
            validate_destination_identity_alias_evidence(plan, protected)
            stored = self._evidence.put_artifact(
                project_id,
                artifact_kind=_KIND,
                artifact_id=protected.evidence_id,
                logical_hash=protected.content_hash,
                payload=protected.to_json().encode("utf-8"),
            )
            return replace(
                plan,
                protected_identity_alias_artifact_hash=stored.artifact_hash,
                identity_alias_evidence=None,
            )
        except Exception as error:
            raise WorkspaceError(
                "Protected destination identity aliases could not be saved; check matching again"
            ) from error

    def read(
        self, project_id: str, plan: DestinationMatchPlan
    ) -> DestinationIdentityAliasEvidence | None:
        if not plan.identity_alias_bindings:
            return None
        if (
            plan.identity_alias_evidence_id is None
            or plan.identity_alias_evidence_hash is None
            or plan.protected_identity_alias_artifact_hash is None
        ):
            raise WorkspaceError(
                "Protected destination identity aliases are unavailable; check matching again"
            )
        try:
            storage_key = self._evidence.artifact_storage_key(
                project_id,
                artifact_kind=_KIND,
                artifact_id=plan.identity_alias_evidence_id,
                logical_hash=plan.identity_alias_evidence_hash,
            )
            raw = self._evidence.read(
                project_id,
                storage_key=storage_key,
                logical_hash=plan.identity_alias_evidence_hash,
                expected_artifact_hash=plan.protected_identity_alias_artifact_hash,
            )
            protected = DestinationIdentityAliasEvidence.from_json(raw.decode("utf-8"))
            validate_destination_identity_alias_evidence(plan, protected)
            return protected
        except Exception as error:
            raise WorkspaceError(
                "Protected destination identity aliases are unavailable or changed; check matching again"
            ) from error

    def put_rules(
        self,
        project_id: str,
        *,
        owner_key: str,
        values: tuple[DestinationIdentityAliasRule, ...],
    ) -> ProtectedIdentityAliasRulesReference:
        """Encrypt reusable choices under stable source and destination meaning."""

        ordered = tuple(sorted(values, key=lambda item: item.model))
        if (
            not ordered
            or len(ordered) > 100
            or len({item.model for item in ordered}) != len(ordered)
        ):
            raise WorkspaceError(
                "Saved identity alias rules must name distinct selected record types"
            )
        payload = {
            "contract_version": 1,
            "owner_key": owner_key,
            "values": [item.to_dict() for item in ordered],
        }
        logical_hash = content_hash(payload)
        rules_id = str(uuid4())
        try:
            ProtectedIdentityAliasRulesReference(
                rules_id, logical_hash, logical_hash, owner_key
            )
            stored = self._evidence.put_artifact(
                project_id,
                artifact_kind=_RULES_KIND,
                artifact_id=rules_id,
                logical_hash=logical_hash,
                payload=canonical_json(payload).encode("utf-8"),
            )
            return ProtectedIdentityAliasRulesReference(
                rules_id, logical_hash, stored.artifact_hash, owner_key
            )
        except Exception as error:
            raise WorkspaceError(
                "Saved identity alias rules could not be protected"
            ) from error

    def read_rules(
        self,
        project_id: str,
        reference: ProtectedIdentityAliasRulesReference,
        *,
        owner_key: str,
    ) -> tuple[DestinationIdentityAliasRule, ...]:
        """Require the same stable owner before opening portable saved choices."""

        if reference.owner_key != owner_key:
            raise WorkspaceError(
                "Saved identity alias choices belong to different source or destination settings; review them again"
            )
        try:
            storage_key = self._evidence.artifact_storage_key(
                project_id,
                artifact_kind=_RULES_KIND,
                artifact_id=reference.rules_id,
                logical_hash=reference.rules_hash,
            )
            raw = self._evidence.read(
                project_id,
                storage_key=storage_key,
                logical_hash=reference.rules_hash,
                expected_artifact_hash=reference.artifact_hash,
            )
            payload = json.loads(raw)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"contract_version", "owner_key", "values"}
                or type(payload["contract_version"]) is not int
                or payload["contract_version"] != 1
                or payload["owner_key"] != owner_key
                or content_hash(payload) != reference.rules_hash
                or not isinstance(payload["values"], list)
                or not 1 <= len(payload["values"]) <= 100
            ):
                raise ValueError("Saved identity alias rule bindings are invalid")
            values = tuple(
                DestinationIdentityAliasRule.from_dict(item)
                for item in payload["values"]
            )
            if values != tuple(sorted(values, key=lambda item: item.model)) or len(
                {item.model for item in values}
            ) != len(values):
                raise ValueError(
                    "Saved identity alias rules are not unique and ordered"
                )
            return values
        except Exception as error:
            raise WorkspaceError(
                "Saved identity alias choices are unavailable or changed; review them again"
            ) from error
