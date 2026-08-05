"""Stable validation evidence and acknowledgement identities."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import json
from typing import Any, Mapping

from ...serialization import canonical_json as _canonical_json
from ...serialization import content_hash as _content_hash
from ...serialization import portable as _portable


MAPPING_VALIDATOR_VERSION = "6.0.0"


class MappingValidationStatus(StrEnum):
    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    INVALID = "INVALID"



@dataclass(frozen=True, slots=True)
class MappingValidationIssue:
    code: str
    severity: str
    path: str
    message: str
    remediation: str
    dataset_id: str | None = None
    source_column_key: str | None = None
    target_model: str | None = None
    target_field: str | None = None


@dataclass(frozen=True, slots=True)
class DeferredRuntimeCheck:
    code: str
    dataset_id: str
    message: str


@dataclass(frozen=True, slots=True)
class MappingValidationResult:
    mapping_content_hash: str
    source_selection_hash: str
    schema_hash: str
    status: MappingValidationStatus
    issues: tuple[MappingValidationIssue, ...]
    coverage: tuple[Mapping[str, Any], ...]
    deferred_runtime_checks: tuple[DeferredRuntimeCheck, ...]
    validator_version: str = MAPPING_VALIDATOR_VERSION
    contract_version: int = 1

    @property
    def validation_hash(self) -> str:
        return _content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "validator_version": self.validator_version,
            "mapping_content_hash": self.mapping_content_hash,
            "source_selection_hash": self.source_selection_hash,
            "schema_hash": self.schema_hash,
            "status": self.status.value,
            "issues": [_portable(asdict(item)) for item in self.issues],
            "coverage": [_portable(dict(item)) for item in self.coverage],
            "deferred_runtime_checks": [
                _portable(asdict(item)) for item in self.deferred_runtime_checks
            ],
        }
        if include_hash:
            payload["validation_hash"] = self.validation_hash
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "MappingValidationResult":
        payload = json.loads(value)
        result = cls(
            contract_version=int(payload["contract_version"]),
            validator_version=str(payload["validator_version"]),
            mapping_content_hash=str(payload["mapping_content_hash"]),
            source_selection_hash=str(payload["source_selection_hash"]),
            schema_hash=str(payload["schema_hash"]),
            status=MappingValidationStatus(payload["status"]),
            issues=tuple(
                MappingValidationIssue(**item) for item in payload["issues"]
            ),
            coverage=tuple(payload["coverage"]),
            deferred_runtime_checks=tuple(
                DeferredRuntimeCheck(**item)
                for item in payload["deferred_runtime_checks"]
            ),
        )
        if payload.get("validation_hash") != result.validation_hash:
            raise ValueError("Mapping-validation hash is invalid")
        return result



def mapping_issue_fingerprint(issue: MappingValidationIssue) -> str:
    """Return the stable acknowledgement key for one validation issue."""

    return _content_hash(
        {
            "code": issue.code,
            "path": issue.path,
            "message": issue.message,
        }
    )
