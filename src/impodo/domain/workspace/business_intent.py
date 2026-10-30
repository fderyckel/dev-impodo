"""Portable business expectations and immutable recording evidence.

Unlisted models permit no writes. A null field or relationship limit adds no
restriction to the technical scope; an empty list permits none. Count limits
are expectations, never permission, and saving them approves no comparison.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from impodo.domain.project.foundation import require_hash, require_uuid
from impodo.domain.serialization import canonical_json, content_hash
from impodo.domain.shared.access import ActorIdentity

BUSINESS_INTENT_VERSION = 1
MAX_INTENT_MODELS = 100
MAX_INTENT_FIELDS = 1_000
MAX_INTENT_BYTES = 1_048_576
_FIELD = re.compile(r"[a-z_][a-z0-9_]{0,199}\Z")
_MODEL = re.compile(r"[a-z_][a-z0-9_.]{0,127}\Z")


class BusinessIntentError(ValueError):
    """Reject ambiguous expectations or a widened technical scope."""


class BusinessWriteMode(StrEnum):
    NONE = "none"
    CREATE = "create"
    UPDATE = "update"
    UPSERT = "upsert"

    @property
    def creates(self) -> bool:
        return self in {self.CREATE, self.UPSERT}

    @property
    def updates(self) -> bool:
        return self in {self.UPDATE, self.UPSERT}


class BusinessRelationshipOperation(StrEnum):
    REPLACE = "replace"
    ADD = "add"
    REMOVE = "remove"


@dataclass(frozen=True, slots=True)
class ExpectedCount:
    """Keep unspecified limits distinct from an explicit zero expectation."""

    minimum: int | None = None
    maximum: int | None = None

    def __post_init__(self) -> None:
        if any(
            value is not None
            and (type(value) is not int or not 0 <= value <= 2**63 - 1)
            for value in (self.minimum, self.maximum)
        ):
            raise BusinessIntentError(
                "Expected counts must be nonnegative whole numbers"
            )
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise BusinessIntentError("An expected minimum cannot exceed its maximum")


@dataclass(frozen=True, slots=True, order=True)
class BusinessRelationshipRule:
    """Permit only named authored relationship operations, not raw Odoo commands."""

    field: str
    operations: tuple[BusinessRelationshipOperation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or not _FIELD.fullmatch(self.field):
            raise BusinessIntentError("Business relationship field is invalid")
        operations = tuple(
            BusinessRelationshipOperation(item) for item in self.operations
        )
        if not operations or len(set(operations)) != len(operations):
            raise BusinessIntentError(
                "Relationship operations must be nonempty and unique"
            )
        object.__setattr__(self, "operations", tuple(sorted(operations)))


def _fields(values: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    if (
        isinstance(values, str)
        or len(values) > MAX_INTENT_FIELDS
        or any(
            not isinstance(value, str) or not _FIELD.fullmatch(value)
            for value in values
        )
    ):
        raise BusinessIntentError(
            "Business field limits are invalid or exceed their limit"
        )
    if len(set(values)) != len(values):
        raise BusinessIntentError("Business field limits contain duplicates")
    return tuple(sorted(values))


def _relationships(
    values: tuple[BusinessRelationshipRule, ...] | None,
) -> tuple[BusinessRelationshipRule, ...] | None:
    if values is None:
        return None
    if len(values) > MAX_INTENT_FIELDS or len({item.field for item in values}) != len(
        values
    ):
        raise BusinessIntentError("Business relationships must be bounded and unique")
    return tuple(sorted(values))


@dataclass(frozen=True, slots=True)
class BusinessModelIntent:
    """Restrict explicit create and update effects for one business record type."""

    model: str
    mode: BusinessWriteMode
    create_fields: tuple[str, ...] | None = None
    update_fields: tuple[str, ...] | None = None
    create_relationships: tuple[BusinessRelationshipRule, ...] | None = None
    update_relationships: tuple[BusinessRelationshipRule, ...] | None = None
    creates: ExpectedCount = field(default_factory=ExpectedCount)
    updates: ExpectedCount = field(default_factory=ExpectedCount)

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not _MODEL.fullmatch(self.model):
            raise BusinessIntentError("Business record type is invalid")
        object.__setattr__(self, "mode", BusinessWriteMode(self.mode))
        for name in ("create_fields", "update_fields"):
            object.__setattr__(self, name, _fields(getattr(self, name)))
        for name in ("create_relationships", "update_relationships"):
            object.__setattr__(self, name, _relationships(getattr(self, name)))
        for allowed, fields, relationships, count in (
            (
                self.mode.creates,
                self.create_fields,
                self.create_relationships,
                self.creates,
            ),
            (
                self.mode.updates,
                self.update_fields,
                self.update_relationships,
                self.updates,
            ),
        ):
            if not isinstance(count, ExpectedCount):
                raise BusinessIntentError(
                    "Expected counts require a bounded count contract"
                )
            if not allowed and (fields or relationships or (count.minimum or 0) > 0):
                raise BusinessIntentError(
                    "Business expectations contradict the allowed write mode"
                )


@dataclass(frozen=True, slots=True)
class BusinessIntent:
    """Reusable, closed business meaning without workspace or target identities."""

    models: tuple[BusinessModelIntent, ...]
    contract_version: int = BUSINESS_INTENT_VERSION

    def __post_init__(self) -> None:
        if (
            type(self.contract_version) is not int
            or self.contract_version != BUSINESS_INTENT_VERSION
            or len(self.models) > MAX_INTENT_MODELS
        ):
            raise BusinessIntentError(
                "Business intent version or model limit is unsupported"
            )
        if len({item.model for item in self.models}) != len(self.models):
            raise BusinessIntentError("Review each business record type only once")
        object.__setattr__(
            self, "models", tuple(sorted(self.models, key=lambda item: item.model))
        )
        if len(self.to_json().encode("utf-8")) > MAX_INTENT_BYTES:
            raise BusinessIntentError("Business intent exceeds its evidence limit")

    @property
    def content_hash(self) -> str:
        return content_hash(asdict(self))

    def to_json(self) -> str:
        return canonical_json(asdict(self))

    @classmethod
    def from_json(cls, value: str) -> BusinessIntent:
        if len(value.encode("utf-8")) > MAX_INTENT_BYTES:
            raise BusinessIntentError("Business intent exceeds its evidence limit")
        raw = json.loads(value)
        if (
            not isinstance(raw, dict)
            or set(raw) != {"models", "contract_version"}
            or not isinstance(raw["models"], list)
            or len(raw["models"]) > MAX_INTENT_MODELS
        ):
            raise BusinessIntentError("Business intent fields are invalid")
        models = []
        for item in raw["models"]:
            if not isinstance(item, dict) or set(item) != {
                "model",
                "mode",
                "create_fields",
                "update_fields",
                "create_relationships",
                "update_relationships",
                "creates",
                "updates",
            }:
                raise BusinessIntentError("Business record expectations are invalid")
            data = dict(item)
            if not isinstance(data["model"], str) or not isinstance(data["mode"], str):
                raise BusinessIntentError(
                    "Business record identity and mode must be text"
                )
            for name in ("creates", "updates"):
                if not isinstance(data[name], dict) or set(data[name]) != {
                    "minimum",
                    "maximum",
                }:
                    raise BusinessIntentError("Expected count fields are invalid")
                data[name] = ExpectedCount(**data[name])
            for name in ("create_fields", "update_fields"):
                if data[name] is not None and not isinstance(data[name], list):
                    raise BusinessIntentError(
                        "Business field limits must be a list or null"
                    )
            for name in ("create_relationships", "update_relationships"):
                if data[name] is not None:
                    if (
                        not isinstance(data[name], list)
                        or len(data[name]) > MAX_INTENT_FIELDS
                        or any(
                            not isinstance(rule, dict)
                            or set(rule) != {"field", "operations"}
                            or not isinstance(rule["operations"], list)
                            or not isinstance(rule["field"], str)
                            for rule in data[name]
                        )
                    ):
                        raise BusinessIntentError(
                            "Business relationship limits are invalid"
                        )
                    data[name] = tuple(
                        BusinessRelationshipRule(**rule) for rule in data[name]
                    )
            models.append(BusinessModelIntent(**data))
        return cls(tuple(models), raw["contract_version"])


@dataclass(frozen=True, slots=True)
class BusinessIntentBinding:
    """Identify current technical inputs without turning them into portable intent."""

    mapping_hash: str
    source_selection_hash: str
    schema_hash: str
    governance_hash: str | None
    validation_hash: str
    scope_hash: str

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if value is not None:
                require_hash(value, name)


@dataclass(frozen=True, slots=True)
class BusinessIntentRevision:
    """Record one explicit expectation; no comparison, assessment or approval."""

    workspace_id: str
    version: int
    operation_id: str
    intent: BusinessIntent
    binding: BusinessIntentBinding
    author: ActorIdentity
    recorded_at: datetime

    def __post_init__(self) -> None:
        require_uuid(self.workspace_id, "workspace_id")
        if (
            str(UUID(self.operation_id)) != self.operation_id
            or type(self.version) is not int
            or not 1 <= self.version <= 2**31 - 1
        ):
            raise BusinessIntentError("Business intent revision identity is invalid")
        if self.recorded_at.tzinfo is None or self.recorded_at.utcoffset() is None:
            raise BusinessIntentError(
                "Business intent recording time must include its timezone"
            )

    @property
    def request_hash(self) -> str:
        return content_hash(
            {
                "intent": asdict(self.intent),
                "binding": asdict(self.binding),
                "parent_version": self.version - 1,
            }
        )

    @property
    def content_hash(self) -> str:
        return content_hash(asdict(self))

    def to_json(self) -> str:
        return canonical_json({**asdict(self), "content_hash": self.content_hash})

    @classmethod
    def from_json(cls, value: str) -> BusinessIntentRevision:
        if len(value.encode("utf-8")) > MAX_INTENT_BYTES + 10_000:
            raise BusinessIntentError(
                "Business intent revision exceeds its evidence limit"
            )
        raw = json.loads(value)
        if not isinstance(raw, dict) or set(raw) != {
            "workspace_id",
            "version",
            "operation_id",
            "intent",
            "binding",
            "author",
            "recorded_at",
            "content_hash",
        }:
            raise BusinessIntentError("Business intent revision fields are invalid")
        expected = raw.pop("content_hash")
        raw["intent"] = BusinessIntent.from_json(canonical_json(raw["intent"]))
        raw["binding"] = BusinessIntentBinding(**raw["binding"])
        raw["author"] = ActorIdentity(**raw["author"])
        raw["recorded_at"] = datetime.fromisoformat(raw["recorded_at"])
        result = cls(**raw)
        if result.content_hash != expected:
            raise BusinessIntentError("Business intent revision hash is invalid")
        return result
