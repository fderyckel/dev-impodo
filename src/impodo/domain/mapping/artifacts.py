"""Versioned mapping revision and submission evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json

from ..serialization import canonical_json as _canonical_json
from ..serialization import portable as _portable
from .contracts import MappingDefinition


@dataclass(frozen=True, slots=True)
class MappingRevision:
    mapping_id: str
    version: int
    parent_version: int | None
    definition: MappingDefinition
    created_at: datetime
    created_by: str

    def to_json(self) -> str:
        return _canonical_json(
            {
                "mapping_id": self.mapping_id,
                "version": self.version,
                "parent_version": self.parent_version,
                "definition": self.definition.to_dict(),
                "created_at": self.created_at.isoformat(),
                "created_by": self.created_by,
            }
        )

    @classmethod
    def from_json(cls, value: str) -> "MappingRevision":
        payload = json.loads(value)
        return cls(
            mapping_id=str(payload["mapping_id"]),
            version=int(payload["version"]),
            parent_version=(
                int(payload["parent_version"])
                if payload.get("parent_version") is not None
                else None
            ),
            definition=MappingDefinition.from_dict(payload["definition"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
            created_by=str(payload["created_by"]),
        )



@dataclass(frozen=True, slots=True)
class MappingSubmission:
    submission_id: str
    mapping_id: str
    version: int
    mapping_content_hash: str
    validation_hash: str
    warning_acknowledgements: tuple[str, ...]
    submitted_at: datetime
    submitted_by: str

    def to_json(self) -> str:
        return _canonical_json(_portable(asdict(self)))

    @classmethod
    def from_json(cls, value: str) -> "MappingSubmission":
        payload = json.loads(value)
        return cls(
            submission_id=str(payload["submission_id"]),
            mapping_id=str(payload["mapping_id"]),
            version=int(payload["version"]),
            mapping_content_hash=str(payload["mapping_content_hash"]),
            validation_hash=str(payload["validation_hash"]),
            warning_acknowledgements=tuple(
                payload.get("warning_acknowledgements", ())
            ),
            submitted_at=datetime.fromisoformat(payload["submitted_at"]),
            submitted_by=str(payload["submitted_by"]),
        )
