"""Define deterministic Stage D validation and acknowledgement evidence.

Layer: domain evidence. Results bind one mapping hash to exact source/schema
hashes, validator version, ordered issues, coverage, and deferred runtime
checks. Warning fingerprints let submission acknowledge meaning rather than a
transient display position.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import json
from typing import Any, Mapping

from ..contracts import CategoricalCoveragePolicy, MAX_VALUE_MAPPINGS
from ...serialization import canonical_json as _canonical_json
from ...serialization import content_hash as _content_hash
from ...serialization import portable as _portable
from impodo.domain.workspace.reference_keys import REFERENCE_POLICY_HASH


MAPPING_VALIDATOR_VERSION = "10.0.0"
MAPPING_VALIDATION_CONTRACT_VERSION = 3
CATEGORICAL_COVERAGE_CONTRACT_VERSION = 1
MAX_CATEGORICAL_EVIDENCE_FIELDS = 10_000
MAX_CATEGORICAL_SOURCE_SNAPSHOTS = 100
MAX_CATEGORICAL_UNCOVERED_VALUES = MAX_VALUE_MAPPINGS + 1


class MappingValidationStatus(StrEnum):
    """Summarize whether errors or reviewable warnings remain."""

    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    INVALID = "INVALID"



@dataclass(frozen=True, slots=True)
class MappingValidationIssue:
    """Describe one stable, actionable semantic finding at a mapping path."""

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
    """Record a valid mapping condition that needs full-row or target evidence."""

    code: str
    dataset_id: str
    message: str


@dataclass(frozen=True, slots=True)
class CategoricalValueCount:
    """One exact nonblank categorical value tuple and its row count."""

    values: tuple[str, ...]
    count: int

    def __post_init__(self) -> None:
        if not self.values or not any(value.strip() for value in self.values):
            raise ValueError("Categorical values must be nonblank")
        if self.count < 1:
            raise ValueError("Categorical value count must be positive")


@dataclass(frozen=True, slots=True)
class CategoricalFieldResult:
    """Coverage outcome for one mapping field under exact provider semantics."""

    path: str
    dataset_id: str
    target_field: str
    policy: str
    source_column_keys: tuple[str, ...]
    distinct_values: tuple[CategoricalValueCount, ...]
    uncovered_values: tuple[tuple[str, ...], ...]
    status: str

    def __post_init__(self) -> None:
        if not self.path or not self.dataset_id or not self.target_field:
            raise ValueError("Categorical field identity is invalid")
        CategoricalCoveragePolicy(self.policy)
        if self.status not in {"COVERED", "UNCOVERED", "UNSUPPORTED"}:
            raise ValueError("Categorical coverage status is unsupported")
        if len(self.distinct_values) > MAX_VALUE_MAPPINGS:
            raise ValueError("Categorical distinct-value evidence is too large")
        if len(self.uncovered_values) > MAX_CATEGORICAL_UNCOVERED_VALUES:
            raise ValueError("Categorical uncovered-value evidence is too large")
        distinct = [item.values for item in self.distinct_values]
        if len(set(distinct)) != len(distinct):
            raise ValueError("Categorical distinct-value evidence is duplicated")
        if self.status == "COVERED" and self.uncovered_values:
            raise ValueError("Covered categorical evidence cannot have gaps")
        if self.status == "UNCOVERED" and not self.uncovered_values:
            raise ValueError("Uncovered categorical evidence must identify gaps")
        if self.status == "UNSUPPORTED" and (
            self.distinct_values or self.uncovered_values
        ):
            raise ValueError("Unsupported categorical evidence cannot claim results")


@dataclass(frozen=True, slots=True)
class CategoricalCoverageEvidence:
    """Immutable bounded source-domain proof for one exact v11 mapping."""

    mapping_content_hash: str
    effective_source_selection_hash: str
    source_snapshot_hashes: tuple[Mapping[str, str], ...]
    scan_contract_hash: str
    provider_and_normalization_semantics_hash: str
    target_schema_dependency_hash: str
    target_reference_evidence: Mapping[str, Any] | None
    field_results: tuple[CategoricalFieldResult, ...]
    contract_version: int = CATEGORICAL_COVERAGE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CATEGORICAL_COVERAGE_CONTRACT_VERSION:
            raise ValueError("Categorical coverage contract is unsupported")
        if len(self.source_snapshot_hashes) > MAX_CATEGORICAL_SOURCE_SNAPSHOTS:
            raise ValueError("Categorical source-snapshot evidence is too large")
        if len(self.field_results) > MAX_CATEGORICAL_EVIDENCE_FIELDS:
            raise ValueError("Categorical field evidence is too large")
        snapshot_ids: list[str] = []
        for item in self.source_snapshot_hashes:
            if set(item) != {"dataset_id", "logical_hash", "parquet_sha256"}:
                raise ValueError("Categorical source-snapshot fields are invalid")
            snapshot_ids.append(str(item["dataset_id"]))
        if len(set(snapshot_ids)) != len(snapshot_ids):
            raise ValueError("Categorical source-snapshot evidence is duplicated")
        field_paths = [item.path for item in self.field_results]
        if len(set(field_paths)) != len(field_paths):
            raise ValueError("Categorical field evidence is duplicated")
        if (
            self.target_reference_evidence is not None
            and not isinstance(self.target_reference_evidence, Mapping)
        ):
            raise ValueError("Categorical target-reference evidence is invalid")

    @property
    def content_hash(self) -> str:
        return _content_hash(self.to_dict(include_hash=False))

    @property
    def recipe_eligible(self) -> bool:
        """Return whether every declared domain has complete reusable proof."""

        return all(item.status == "COVERED" for item in self.field_results)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "mapping_content_hash": self.mapping_content_hash,
            "effective_source_selection_hash": self.effective_source_selection_hash,
            "source_snapshot_hashes": [
                _portable(dict(item)) for item in self.source_snapshot_hashes
            ],
            "scan_contract_hash": self.scan_contract_hash,
            "provider_and_normalization_semantics_hash": (
                self.provider_and_normalization_semantics_hash
            ),
            "target_schema_dependency_hash": self.target_schema_dependency_hash,
            "target_reference_evidence": (
                _portable(dict(self.target_reference_evidence))
                if self.target_reference_evidence is not None
                else None
            ),
            "field_results": [
                _portable(asdict(item)) for item in self.field_results
            ],
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CategoricalCoverageEvidence":
        if set(payload) != {
            "contract_version",
            "mapping_content_hash",
            "effective_source_selection_hash",
            "source_snapshot_hashes",
            "scan_contract_hash",
            "provider_and_normalization_semantics_hash",
            "target_schema_dependency_hash",
            "target_reference_evidence",
            "field_results",
            "content_hash",
        }:
            raise ValueError("Categorical-coverage fields are invalid")
        for item in payload["field_results"]:
            if not isinstance(item, Mapping) or set(item) != {
                "path",
                "dataset_id",
                "target_field",
                "policy",
                "source_column_keys",
                "distinct_values",
                "uncovered_values",
                "status",
            }:
                raise ValueError("Categorical field-result fields are invalid")
            if any(
                not isinstance(value, Mapping)
                or set(value) != {"values", "count"}
                for value in item["distinct_values"]
            ):
                raise ValueError("Categorical value-count fields are invalid")
        evidence = cls(
            contract_version=int(payload["contract_version"]),
            mapping_content_hash=str(payload["mapping_content_hash"]),
            effective_source_selection_hash=str(
                payload["effective_source_selection_hash"]
            ),
            source_snapshot_hashes=tuple(payload["source_snapshot_hashes"]),
            scan_contract_hash=str(payload["scan_contract_hash"]),
            provider_and_normalization_semantics_hash=str(
                payload["provider_and_normalization_semantics_hash"]
            ),
            target_schema_dependency_hash=str(
                payload["target_schema_dependency_hash"]
            ),
            target_reference_evidence=payload["target_reference_evidence"],
            field_results=tuple(
                CategoricalFieldResult(
                    path=str(item["path"]),
                    dataset_id=str(item["dataset_id"]),
                    target_field=str(item["target_field"]),
                    policy=str(item["policy"]),
                    source_column_keys=tuple(item["source_column_keys"]),
                    distinct_values=tuple(
                        CategoricalValueCount(
                            values=tuple(value["values"]),
                            count=int(value["count"]),
                        )
                        for value in item["distinct_values"]
                    ),
                    uncovered_values=tuple(
                        tuple(value) for value in item["uncovered_values"]
                    ),
                    status=str(item["status"]),
                )
                for item in payload["field_results"]
            ),
        )
        if payload.get("content_hash") != evidence.content_hash:
            raise ValueError("Categorical-coverage hash is invalid")
        return evidence


@dataclass(frozen=True, slots=True)
class MappingValidationResult:
    """Bind deterministic findings and coverage to one exact mapping context."""

    mapping_content_hash: str
    source_selection_hash: str
    schema_hash: str
    status: MappingValidationStatus
    issues: tuple[MappingValidationIssue, ...]
    coverage: tuple[Mapping[str, Any], ...]
    deferred_runtime_checks: tuple[DeferredRuntimeCheck, ...]
    categorical_coverage: CategoricalCoverageEvidence | None = None
    reference_policy_hash: str = REFERENCE_POLICY_HASH
    validator_version: str = MAPPING_VALIDATOR_VERSION
    contract_version: int = MAPPING_VALIDATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            self.contract_version != MAPPING_VALIDATION_CONTRACT_VERSION
            or self.validator_version != MAPPING_VALIDATOR_VERSION
        ):
            raise ValueError(
                "Mapping validation does not match the current contract"
            )

    @property
    def validation_hash(self) -> str:
        """Return the immutable identity used by mapping submission."""

        return _content_hash(self.to_dict(include_hash=False))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        """Return the stable portable validation payload."""

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
        payload["categorical_coverage"] = (
            self.categorical_coverage.to_dict()
            if self.categorical_coverage is not None
            else None
        )
        payload["reference_policy_hash"] = self.reference_policy_hash
        if include_hash:
            payload["validation_hash"] = self.validation_hash
        return payload

    def to_json(self) -> str:
        """Serialize validation evidence with its content hash."""

        return _canonical_json(self.to_dict())

    @classmethod
    def from_json(cls, value: str) -> "MappingValidationResult":
        """Restore validation evidence and reject hash tampering."""

        payload = json.loads(value)
        contract_version = int(payload["contract_version"])
        if contract_version != MAPPING_VALIDATION_CONTRACT_VERSION:
            raise ValueError(
                "Mapping validation does not match the current contract"
            )
        expected_fields = {
            "contract_version",
            "validator_version",
            "mapping_content_hash",
            "source_selection_hash",
            "schema_hash",
            "status",
            "issues",
            "coverage",
            "deferred_runtime_checks",
            "categorical_coverage",
            "reference_policy_hash",
            "validation_hash",
        }
        if set(payload) != expected_fields:
            raise ValueError("Mapping-validation fields are invalid")
        result = cls(
            contract_version=contract_version,
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
            categorical_coverage=(
                CategoricalCoverageEvidence.from_dict(
                    payload["categorical_coverage"]
                )
                if payload.get("categorical_coverage") is not None
                else None
            ),
            reference_policy_hash=str(payload["reference_policy_hash"]),
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
