"""Define Stage C portable business-key governance for captured Odoo models.

Layer: domain. Business keys are functional matching identities expressed only
with model and field names; they never contain numeric Odoo IDs. One immutable
``SchemaGovernance`` revision binds the full confirmed key set to the exact
captured schema hash.

See ``docs/architecture/python-code-map.md`` and
``tests/test_business_keys.py``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
import json
from typing import Any

from ..serialization import canonical_json as _canonical_json
from ..serialization import content_hash as _content_hash


class BusinessKeyStatus(StrEnum):
    """Distinguish suggested key shapes from explicitly governed identities."""

    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"



@dataclass(frozen=True, slots=True)
class BusinessKeyDefinition:
    """Governed natural identity for one captured Odoo model."""

    key_id: str
    model: str
    key_fields: tuple[str, ...]
    scope_fields: tuple[str, ...] = ()
    description: str = ""
    status: BusinessKeyStatus = BusinessKeyStatus.CANDIDATE

    def __post_init__(self) -> None:
        key_id = self.key_id.strip()
        model = self.model.strip()
        description = self.description.strip()
        key_fields = tuple(item.strip() for item in self.key_fields)
        scope_fields = tuple(item.strip() for item in self.scope_fields)
        if not key_id or not model:
            raise ValueError("Business-key ID and model must not be blank")
        if len(key_id) > 200 or len(model) > 200:
            raise ValueError("Business-key ID or model is too long")
        if len(description) > 1000:
            raise ValueError("Business-key description is too long")
        if not key_fields:
            raise ValueError("A business key requires at least one key field")
        all_fields = (*key_fields, *scope_fields)
        if any(not item.strip() for item in all_fields):
            raise ValueError("Business-key fields must not be blank")
        if len(set(all_fields)) != len(all_fields):
            raise ValueError("Business-key fields and scope must be unique")
        object.__setattr__(self, "key_id", key_id)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "key_fields", key_fields)
        object.__setattr__(self, "scope_fields", scope_fields)
        object.__setattr__(self, "status", BusinessKeyStatus(self.status))


@dataclass(frozen=True, slots=True)
class SchemaGovernance:
    """Versioned model scope and business keys bound to a schema catalog."""

    governance_id: str
    version: int
    project_id: str
    catalog_hash: str
    permitted_models: tuple[str, ...]
    business_keys: tuple[BusinessKeyDefinition, ...]
    recorded_at: datetime
    recorded_by: str

    @property
    def content_hash(self) -> str:
        """Bind model scope, keys, provenance, and revision metadata."""

        return _content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        """Return the deterministic portable governance payload."""

        payload: dict[str, Any] = {
            "governance_id": self.governance_id,
            "version": self.version,
            "project_id": self.project_id,
            "catalog_hash": self.catalog_hash,
            "permitted_models": list(self.permitted_models),
            "business_keys": [
                {
                    **asdict(item),
                    "status": item.status.value,
                }
                for item in self.business_keys
            ],
            "recorded_at": self.recorded_at.isoformat(),
            "recorded_by": self.recorded_by,
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        """Serialize governance with its verified content hash."""

        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "SchemaGovernance":
        """Restore governance and reject a mismatched content hash."""

        payload = json.loads(value)
        result = cls(
            governance_id=str(payload["governance_id"]),
            version=int(payload["version"]),
            project_id=str(payload["project_id"]),
            catalog_hash=str(payload["catalog_hash"]),
            permitted_models=tuple(payload["permitted_models"]),
            business_keys=tuple(
                BusinessKeyDefinition(
                    key_id=str(item["key_id"]),
                    model=str(item["model"]),
                    key_fields=tuple(item["key_fields"]),
                    scope_fields=tuple(item.get("scope_fields", ())),
                    description=str(item.get("description", "")),
                    status=BusinessKeyStatus(item["status"]),
                )
                for item in payload["business_keys"]
            ),
            recorded_at=datetime.fromisoformat(payload["recorded_at"]),
            recorded_by=str(payload["recorded_by"]),
        )
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Schema-governance content hash is invalid")
        return result
