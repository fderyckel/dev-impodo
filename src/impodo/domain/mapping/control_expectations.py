"""Edition-local evidence for expected values of reusable controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from uuid import UUID

from impodo.domain.shared.access import ActorIdentity
from ..serialization import content_hash


EDITION_CONTROL_EXPECTATION_CONTRACT_VERSION = 2


@dataclass(frozen=True, slots=True)
class EditionControlExpectation:
    """Bind a fresh expected decimal to one workspace edition and actor."""

    workspace_id: str
    logical_control_id: str
    expected_value: str
    source: str
    reason: str
    actor: ActorIdentity
    recorded_at: datetime
    contract_version: int = EDITION_CONTROL_EXPECTATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != EDITION_CONTROL_EXPECTATION_CONTRACT_VERSION:
            raise ValueError("Edition control-expectation contract is unsupported")
        try:
            object.__setattr__(self, "workspace_id", str(UUID(self.workspace_id)))
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("Edition control expectation workspace is invalid") from error
        logical_control_id = self.logical_control_id.strip()
        source = self.source.strip()
        reason = self.reason.strip()
        if not logical_control_id or len(logical_control_id) > 300:
            raise ValueError("Edition control identifier is invalid")
        if not source or len(source) > 80:
            raise ValueError("Edition control expectation source is invalid")
        if not reason or len(reason) > 1_000:
            raise ValueError("Edition control expectation reason is invalid")
        if self.recorded_at.tzinfo is None:
            raise ValueError("Edition control expectation time must be timezone-aware")
        try:
            expected = Decimal(self.expected_value.strip())
        except (InvalidOperation, AttributeError) as error:
            raise ValueError("Edition control expectation requires a number") from error
        if not expected.is_finite():
            raise ValueError("Edition control expectation requires a finite number")
        object.__setattr__(self, "logical_control_id", logical_control_id)
        object.__setattr__(self, "expected_value", format(expected, "f"))
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "reason", reason)

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "workspace_id": self.workspace_id,
            "logical_control_id": self.logical_control_id,
            "expected_value": self.expected_value,
            "source": self.source,
            "reason": self.reason,
            "actor": {
                "issuer": self.actor.issuer,
                "subject_id": self.actor.subject_id,
                "display_name": self.actor.display_name,
            },
            "recorded_at": self.recorded_at.isoformat(),
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EditionControlExpectation":
        if set(payload) != {
            "contract_version",
            "workspace_id",
            "logical_control_id",
            "expected_value",
            "source",
            "reason",
            "actor",
            "recorded_at",
            "content_hash",
        }:
            raise ValueError("Edition control-expectation fields are invalid")
        actor_payload = payload["actor"]
        if not isinstance(actor_payload, Mapping) or set(actor_payload) != {
            "issuer",
            "subject_id",
            "display_name",
        }:
            raise ValueError("Edition control-expectation actor is invalid")
        expectation = cls(
            contract_version=int(payload["contract_version"]),
            workspace_id=str(payload["workspace_id"]),
            logical_control_id=str(payload["logical_control_id"]),
            expected_value=str(payload["expected_value"]),
            source=str(payload["source"]),
            reason=str(payload["reason"]),
            actor=ActorIdentity(
                issuer=str(actor_payload["issuer"]),
                subject_id=str(actor_payload["subject_id"]),
                display_name=str(actor_payload["display_name"]),
            ),
            recorded_at=datetime.fromisoformat(str(payload["recorded_at"])),
        )
        if payload["content_hash"] != expectation.content_hash:
            raise ValueError("Edition control-expectation hash is invalid")
        return expectation
