"""Bind reviewed workspaces to explicitly declared Odoo database generations.

The generation is local control-plane authority, not an observed server UUID.
One Organization shares it across Projects, Companies and connection modes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit

from impodo.domain.project.foundation import (
    require_aware,
    require_hash,
    require_uuid,
    required_text,
)
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import ActorIdentity


class OdooDatabaseBindingError(ValueError):
    """A database authority cannot admit the requested local or remote action."""


class OdooDatabaseGenerationConflict(OdooDatabaseBindingError):
    """A declaration or reviewed workspace belongs to a different generation."""


class OdooDatabaseBindingRole(StrEnum):
    """Separate source capture authority from destination execution authority."""

    SOURCE = "SOURCE"
    DESTINATION = "DESTINATION"


def database_scope_hash(base_url: str, database: str) -> str:
    """Canonicalize one endpoint and database without inferring endpoint aliases."""

    endpoint = urlsplit(required_text(base_url, "base_url", maximum=2048))
    if (
        endpoint.scheme.lower() not in {"http", "https"}
        or endpoint.hostname is None
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise OdooDatabaseBindingError("Choose a valid Odoo database endpoint")
    scheme = endpoint.scheme.lower()
    hostname = endpoint.hostname.lower()
    host = f"[{hostname}]" if ":" in hostname else hostname
    try:
        port = endpoint.port
    except ValueError as error:
        raise OdooDatabaseBindingError(
            "Choose a valid Odoo database endpoint"
        ) from error
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    canonical = urlunsplit((scheme, host, endpoint.path.rstrip("/"), "", ""))
    return content_hash(
        {
            "contract_version": 1,
            "kind": "ODOO_DATABASE_SCOPE",
            "base_url": canonical,
            "database": required_text(database, "database", maximum=300),
        }
    )


@dataclass(frozen=True, slots=True)
class OdooDatabaseGeneration:
    """One immutable initial or explicitly declared database generation."""

    organization_id: str
    scope_hash: str
    generation: int
    previous_generation_hash: str | None = None
    operation_id: str | None = None
    actor: ActorIdentity | None = None
    declared_at: datetime | None = None
    reason: str = "Initial database binding"
    contract_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "organization_id",
            required_text(self.organization_id, "organization_id", maximum=200),
        )
        require_hash(self.scope_hash, "scope_hash")
        if (
            type(self.contract_version) is not int
            or self.contract_version != 1
            or type(self.generation) is not int
            or self.generation < 0
        ):
            raise OdooDatabaseBindingError("The database generation is invalid")
        if self.generation == 0:
            if (
                any(
                    value is not None
                    for value in (
                        self.previous_generation_hash,
                        self.operation_id,
                        self.actor,
                        self.declared_at,
                    )
                )
                or self.reason != "Initial database binding"
            ):
                raise OdooDatabaseBindingError(
                    "The initial database generation must be deterministic"
                )
        else:
            if (
                self.previous_generation_hash is None
                or self.operation_id is None
                or self.actor is None
                or self.declared_at is None
            ):
                raise OdooDatabaseBindingError(
                    "A database reset declaration needs complete audit evidence"
                )
            require_hash(self.previous_generation_hash, "previous_generation_hash")
            require_uuid(self.operation_id, "operation_id")
            require_aware(self.declared_at, "declared_at")
            object.__setattr__(
                self, "reason", required_text(self.reason, "reason", maximum=1000)
            )

    @property
    def content_hash(self) -> str:
        """Bind one complete declared generation without a server-ID claim."""

        return content_hash(self.to_dict())

    @property
    def generation_hash(self) -> str:
        """Expose the same immutable hash used by reviewed workspace bindings."""

        return self.content_hash

    def to_dict(self) -> dict[str, object]:
        """Serialize only the current generation contract."""

        payload = asdict(self)
        payload["declared_at"] = (
            self.declared_at.isoformat() if self.declared_at is not None else None
        )
        return payload

    def to_json(self) -> str:
        """Encode one immutable generation for registry integrity checking."""

        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> OdooDatabaseGeneration:
        """Read the current generation shape without accepting retired formats."""

        payload = json.loads(value)
        if not isinstance(payload, dict) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise OdooDatabaseBindingError("The stored database generation is invalid")
        actor = payload["actor"]
        return cls(
            organization_id=payload["organization_id"],
            scope_hash=payload["scope_hash"],
            generation=payload["generation"],
            previous_generation_hash=payload["previous_generation_hash"],
            operation_id=payload["operation_id"],
            actor=ActorIdentity(**actor) if actor is not None else None,
            declared_at=datetime.fromisoformat(payload["declared_at"])
            if payload["declared_at"] is not None
            else None,
            reason=payload["reason"],
            contract_version=payload["contract_version"],
        )


@dataclass(frozen=True, slots=True)
class OdooDatabaseBinding:
    """Retain the exact authority admitted by one reviewed workspace and role."""

    workspace_id: str
    project_id: str
    organization_id: str
    role: OdooDatabaseBindingRole
    scope_hash: str
    generation_hash: str
    actor: ActorIdentity
    bound_at: datetime
    operation_id: str
    contract_version: int = 1

    def __post_init__(self) -> None:
        for field in ("workspace_id", "project_id", "operation_id"):
            require_uuid(getattr(self, field), field)
        object.__setattr__(
            self,
            "organization_id",
            required_text(self.organization_id, "organization_id", maximum=200),
        )
        object.__setattr__(self, "role", OdooDatabaseBindingRole(self.role))
        require_hash(self.scope_hash, "scope_hash")
        require_hash(self.generation_hash, "generation_hash")
        require_aware(self.bound_at, "bound_at")
        if type(self.contract_version) is not int or self.contract_version != 1:
            raise OdooDatabaseBindingError("The workspace database binding is invalid")

    @property
    def content_hash(self) -> str:
        """Identify the immutable reviewed binding independently of its generation."""

        return content_hash(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        """Preserve exact audit provenance under the current binding contract."""

        return {
            **asdict(self),
            "role": self.role.value,
            "bound_at": self.bound_at.isoformat(),
        }

    def to_json(self) -> str:
        """Encode one reviewed binding for its registry integrity hash."""

        return canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> OdooDatabaseBinding:
        """Require the complete current workspace binding shape."""

        payload = json.loads(value)
        if not isinstance(payload, dict) or set(payload) != set(
            cls.__dataclass_fields__
        ):
            raise OdooDatabaseBindingError(
                "The stored workspace database binding is invalid"
            )
        return cls(
            workspace_id=payload["workspace_id"],
            project_id=payload["project_id"],
            organization_id=payload["organization_id"],
            role=OdooDatabaseBindingRole(payload["role"]),
            scope_hash=payload["scope_hash"],
            generation_hash=payload["generation_hash"],
            actor=ActorIdentity(**payload["actor"]),
            bound_at=datetime.fromisoformat(payload["bound_at"]),
            operation_id=payload["operation_id"],
            contract_version=payload["contract_version"],
        )
