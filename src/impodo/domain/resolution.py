"""Bounded entity-resolution contracts and deterministic candidate generation.

Migration stages: E-G. Layer: domain behavior. Fuzzy evaluation only proposes
source-side candidate pairs. It never contacts Odoo, changes canonical staging,
or merges rows. Reviewed decisions and effective rows are separate contracts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from enum import StrEnum
import re
from typing import Any, Iterable, Mapping
import unicodedata
from uuid import UUID

from ..access import ActorIdentity
from ..models import (
    assert_no_numeric_odoo_ids,
    portable_value,
    restore_portable_value,
)
from ..staging_contracts import CanonicalLineage, CanonicalRow, StagingDisposition
from .serialization import content_hash


RESOLUTION_POLICY_CONTRACT_VERSION = 1
RESOLUTION_EVALUATION_CONTRACT_VERSION = 1
RESOLUTION_DECISION_CONTRACT_VERSION = 1
EFFECTIVE_DATASET_CONTRACT_VERSION = 1
RESOLUTION_SCORER_VERSION = 1
MAX_FUZZY_BLOCK_SIZE = 50
MAX_FUZZY_CANDIDATES_PER_ROW = 5
MAX_FUZZY_BLOCKING_FIELDS = 3
MAX_FUZZY_COMPARISON_FIELDS = 5
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_FIELD = re.compile(r"[a-z_][a-z0-9_.]{0,127}")
_SCORE_QUANTUM = Decimal("0.000001")


class SimilarityAlgorithm(StrEnum):
    NORMALIZED_LEVENSHTEIN = "NORMALIZED_LEVENSHTEIN"
    TOKEN_JACCARD = "TOKEN_JACCARD"


class ResolutionDecisionKind(StrEnum):
    SAME_RECORD = "SAME_RECORD"
    KEEP_SEPARATE = "KEEP_SEPARATE"
    SELECT_SOURCE = "SELECT_SOURCE"
    REVIEWER_CORRECTION = "REVIEWER_CORRECTION"


class ResolutionState(StrEnum):
    PASSED_THROUGH = "PASSED_THROUGH"
    KEPT_DISTINCT = "KEPT_DISTINCT"
    CONTRIBUTED_TO_SURVIVOR = "CONTRIBUTED_TO_SURVIVOR"


class FieldProvenanceKind(StrEnum):
    COPIED = "COPIED"
    UNANIMOUS = "UNANIMOUS"
    SELECTED_SOURCE = "SELECTED_SOURCE"
    REVIEWER_CORRECTION = "REVIEWER_CORRECTION"
    STRUCTURAL_AGGREGATE = "STRUCTURAL_AGGREGATE"


@dataclass(frozen=True, slots=True)
class FuzzyComparisonField:
    field: str
    algorithm: SimilarityAlgorithm
    weight: str = "1"

    def __post_init__(self) -> None:
        _field(self.field)
        object.__setattr__(self, "algorithm", SimilarityAlgorithm(self.algorithm))
        weight = _decimal(self.weight, "fuzzy weight")
        if weight <= 0 or weight > 1:
            raise ValueError("Fuzzy weights must be greater than zero and at most one")
        object.__setattr__(self, "weight", format(weight, "f"))

    def to_portable_dict(self) -> dict[str, str]:
        return {
            "field": self.field,
            "algorithm": self.algorithm.value,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FuzzyComparisonField":
        return cls(
            field=str(payload["field"]),
            algorithm=SimilarityAlgorithm(str(payload["algorithm"])),
            weight=str(payload.get("weight", "1")),
        )


@dataclass(frozen=True, slots=True)
class ResolutionRule:
    """One bounded fuzzy and survivor policy for one canonical dataset."""

    rule_id: str
    dataset: str
    blocking_fields: tuple[str, ...]
    comparison_fields: tuple[FuzzyComparisonField, ...]
    candidate_threshold: str
    survivor_fields: tuple[str, ...]
    correctable_fields: tuple[str, ...] = ()
    punctuation: str = " -_.,/'&()"
    max_block_size: int = MAX_FUZZY_BLOCK_SIZE
    max_candidates_per_row: int = MAX_FUZZY_CANDIDATES_PER_ROW

    def __post_init__(self) -> None:
        _uuid(self.rule_id, "resolution rule ID")
        _required_text(self.dataset, "resolution dataset", 200)
        if not 1 <= len(self.blocking_fields) <= MAX_FUZZY_BLOCKING_FIELDS:
            raise ValueError("Resolution rules require one to three blocking fields")
        if len(set(self.blocking_fields)) != len(self.blocking_fields):
            raise ValueError("Resolution blocking fields must be unique")
        for field in self.blocking_fields:
            _field(field)
        if not 1 <= len(self.comparison_fields) <= MAX_FUZZY_COMPARISON_FIELDS:
            raise ValueError("Resolution rules require one to five comparison fields")
        comparison_names = [item.field for item in self.comparison_fields]
        if len(set(comparison_names)) != len(comparison_names):
            raise ValueError("Resolution comparison fields must be unique")
        expected_comparisons = tuple(
            sorted(self.comparison_fields, key=lambda item: item.field)
        )
        if self.comparison_fields != expected_comparisons:
            raise ValueError("Resolution comparison fields must be ordered")
        threshold = _decimal(self.candidate_threshold, "candidate threshold")
        if threshold <= 0 or threshold > 1:
            raise ValueError("Candidate threshold must be greater than zero and at most one")
        object.__setattr__(self, "candidate_threshold", format(threshold, "f"))
        survivor_fields = _ordered_fields(self.survivor_fields, "survivor fields")
        correctable_fields = _ordered_fields(
            self.correctable_fields,
            "correctable fields",
            allow_empty=True,
        )
        if not set(correctable_fields).issubset(set(survivor_fields)):
            raise ValueError("Correctable fields must also be survivor fields")
        object.__setattr__(self, "survivor_fields", survivor_fields)
        object.__setattr__(self, "correctable_fields", correctable_fields)
        if not self.punctuation or len(self.punctuation) > 40:
            raise ValueError("Fuzzy punctuation policy is invalid")
        if not 2 <= self.max_block_size <= MAX_FUZZY_BLOCK_SIZE:
            raise ValueError("Fuzzy block size exceeds the supported bound")
        if not 1 <= self.max_candidates_per_row <= MAX_FUZZY_CANDIDATES_PER_ROW:
            raise ValueError("Fuzzy candidate count exceeds the supported bound")

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "dataset": self.dataset,
            "blocking_fields": list(self.blocking_fields),
            "comparison_fields": [
                item.to_portable_dict() for item in self.comparison_fields
            ],
            "candidate_threshold": self.candidate_threshold,
            "survivor_fields": list(self.survivor_fields),
            "correctable_fields": list(self.correctable_fields),
            "punctuation": self.punctuation,
            "max_block_size": self.max_block_size,
            "max_candidates_per_row": self.max_candidates_per_row,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolutionRule":
        return cls(
            rule_id=str(payload["rule_id"]),
            dataset=str(payload["dataset"]),
            blocking_fields=tuple(str(item) for item in payload.get("blocking_fields", ())),
            comparison_fields=tuple(
                FuzzyComparisonField.from_dict(item)
                for item in payload.get("comparison_fields", ())
            ),
            candidate_threshold=str(payload["candidate_threshold"]),
            survivor_fields=tuple(str(item) for item in payload.get("survivor_fields", ())),
            correctable_fields=tuple(
                str(item) for item in payload.get("correctable_fields", ())
            ),
            punctuation=str(payload.get("punctuation", " -_.,/'&()")),
            max_block_size=int(payload.get("max_block_size", MAX_FUZZY_BLOCK_SIZE)),
            max_candidates_per_row=int(
                payload.get("max_candidates_per_row", MAX_FUZZY_CANDIDATES_PER_ROW)
            ),
        )


@dataclass(frozen=True, slots=True)
class ResolutionPolicy:
    policy_id: str
    project_id: str
    version: int
    parent_version: int | None
    coverage_scope_hash: str
    mapping_hash: str
    schema_hash: str
    reference_bundle_hash: str
    rules: tuple[ResolutionRule, ...]
    contract_version: int = RESOLUTION_POLICY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _uuid(self.policy_id, "resolution policy ID")
        _required_text(self.project_id, "project ID", 200)
        if self.contract_version != RESOLUTION_POLICY_CONTRACT_VERSION:
            raise ValueError("Resolution-policy contract version is unsupported")
        if self.version < 1:
            raise ValueError("Resolution-policy version must be positive")
        if self.parent_version is not None and self.parent_version >= self.version:
            raise ValueError("Resolution-policy parent version is invalid")
        for value, label in (
            (self.coverage_scope_hash, "coverage scope hash"),
            (self.mapping_hash, "mapping hash"),
            (self.schema_hash, "schema hash"),
            (self.reference_bundle_hash, "reference bundle hash"),
        ):
            _hash(value, label)
        expected = tuple(sorted(self.rules, key=lambda item: item.rule_id))
        if self.rules != expected:
            raise ValueError("Resolution rules must use deterministic order")
        if len({item.rule_id for item in self.rules}) != len(self.rules):
            raise ValueError("Resolution rule IDs must be unique")
        if len({item.dataset for item in self.rules}) != len(self.rules):
            raise ValueError("Only one resolution rule is supported per dataset")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_portable_dict(include_hash=False))

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "policy_id": self.policy_id,
            "project_id": self.project_id,
            "version": self.version,
            "parent_version": self.parent_version,
            "coverage_scope_hash": self.coverage_scope_hash,
            "mapping_hash": self.mapping_hash,
            "schema_hash": self.schema_hash,
            "reference_bundle_hash": self.reference_bundle_hash,
            "rules": [item.to_portable_dict() for item in self.rules],
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        assert_no_numeric_odoo_ids(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolutionPolicy":
        result = cls(
            contract_version=int(payload["contract_version"]),
            policy_id=str(payload["policy_id"]),
            project_id=str(payload["project_id"]),
            version=int(payload["version"]),
            parent_version=(
                int(payload["parent_version"])
                if payload.get("parent_version") is not None
                else None
            ),
            coverage_scope_hash=str(payload["coverage_scope_hash"]),
            mapping_hash=str(payload["mapping_hash"]),
            schema_hash=str(payload["schema_hash"]),
            reference_bundle_hash=str(payload["reference_bundle_hash"]),
            rules=tuple(
                ResolutionRule.from_dict(item) for item in payload.get("rules", ())
            ),
        )
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Resolution-policy content hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class ResolutionCandidate:
    candidate_id: str
    rule_id: str
    dataset: str
    left_row_id: str
    right_row_id: str
    block_key: tuple[str, ...]
    score: str
    components: Mapping[str, str]

    def __post_init__(self) -> None:
        for value, label in (
            (self.candidate_id, "candidate ID"),
            (self.left_row_id, "left row ID"),
            (self.right_row_id, "right row ID"),
        ):
            _hash(value, label)
        _uuid(self.rule_id, "resolution rule ID")
        if self.left_row_id >= self.right_row_id:
            raise ValueError("Candidate row IDs must be unique and ordered")
        _required_text(self.dataset, "candidate dataset", 200)
        if not self.block_key or any(not item for item in self.block_key):
            raise ValueError("Candidate block key is incomplete")
        score = _decimal(self.score, "candidate score")
        if score < 0 or score > 1:
            raise ValueError("Candidate score must be between zero and one")
        object.__setattr__(self, "score", _score_text(score))
        normalized = {
            _valid_component_name(field): _score_text(
                _bounded_score(value, "candidate component")
            )
            for field, value in self.components.items()
        }
        if not normalized:
            raise ValueError("Candidate score components are required")
        object.__setattr__(self, "components", normalized)

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "rule_id": self.rule_id,
            "dataset": self.dataset,
            "left_row_id": self.left_row_id,
            "right_row_id": self.right_row_id,
            "block_key": list(self.block_key),
            "score": self.score,
            "components": dict(sorted(self.components.items())),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolutionCandidate":
        return cls(
            candidate_id=str(payload["candidate_id"]),
            rule_id=str(payload["rule_id"]),
            dataset=str(payload["dataset"]),
            left_row_id=str(payload["left_row_id"]),
            right_row_id=str(payload["right_row_id"]),
            block_key=tuple(str(item) for item in payload.get("block_key", ())),
            score=str(payload["score"]),
            components={
                str(field): str(value)
                for field, value in dict(payload.get("components", {})).items()
            },
        )


@dataclass(frozen=True, slots=True)
class ResolutionFinding:
    finding_id: str
    rule_id: str
    dataset: str
    code: str
    message: str
    affected_count: int
    blocking: bool = True

    def __post_init__(self) -> None:
        _hash(self.finding_id, "resolution finding ID")
        _uuid(self.rule_id, "resolution rule ID")
        _required_text(self.dataset, "resolution finding dataset", 200)
        _required_text(self.code, "resolution finding code", 120)
        _required_text(self.message, "resolution finding message", 1_000)
        if self.affected_count < 1:
            raise ValueError("Resolution findings require affected rows")

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "rule_id": self.rule_id,
            "dataset": self.dataset,
            "code": self.code,
            "message": self.message,
            "affected_count": self.affected_count,
            "blocking": self.blocking,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolutionFinding":
        return cls(
            finding_id=str(payload["finding_id"]),
            rule_id=str(payload["rule_id"]),
            dataset=str(payload["dataset"]),
            code=str(payload["code"]),
            message=str(payload["message"]),
            affected_count=int(payload["affected_count"]),
            blocking=bool(payload.get("blocking", True)),
        )


@dataclass(frozen=True, slots=True)
class ResolutionEvaluation:
    project_id: str
    staging_content_hash: str
    policy_hash: str
    candidates: tuple[ResolutionCandidate, ...]
    findings: tuple[ResolutionFinding, ...]
    compared_pair_count: int
    scorer_version: int = RESOLUTION_SCORER_VERSION
    contract_version: int = RESOLUTION_EVALUATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _required_text(self.project_id, "project ID", 200)
        _hash(self.staging_content_hash, "staging content hash")
        _hash(self.policy_hash, "resolution policy hash")
        if self.contract_version != RESOLUTION_EVALUATION_CONTRACT_VERSION:
            raise ValueError("Resolution-evaluation contract version is unsupported")
        if self.scorer_version != RESOLUTION_SCORER_VERSION:
            raise ValueError("Resolution scorer version is unsupported")
        if self.compared_pair_count < len(self.candidates):
            raise ValueError("Compared-pair count cannot be smaller than candidates")
        expected_candidates = tuple(
            sorted(self.candidates, key=lambda item: item.candidate_id)
        )
        expected_findings = tuple(sorted(self.findings, key=lambda item: item.finding_id))
        if self.candidates != expected_candidates or self.findings != expected_findings:
            raise ValueError("Resolution evidence must use deterministic order")
        if len({item.candidate_id for item in self.candidates}) != len(self.candidates):
            raise ValueError("Resolution candidate IDs must be unique")
        degree: dict[str, int] = {}
        for item in self.candidates:
            degree[item.left_row_id] = degree.get(item.left_row_id, 0) + 1
            degree[item.right_row_id] = degree.get(item.right_row_id, 0) + 1
        if any(value > MAX_FUZZY_CANDIDATES_PER_ROW for value in degree.values()):
            raise ValueError("Resolution candidates exceed the per-row bound")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_portable_dict(include_hash=False))

    @property
    def blocked(self) -> bool:
        return any(item.blocking for item in self.findings)

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "scorer_version": self.scorer_version,
            "project_id": self.project_id,
            "staging_content_hash": self.staging_content_hash,
            "policy_hash": self.policy_hash,
            "compared_pair_count": self.compared_pair_count,
            "candidates": [item.to_portable_dict() for item in self.candidates],
            "findings": [item.to_portable_dict() for item in self.findings],
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        assert_no_numeric_odoo_ids(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolutionEvaluation":
        result = cls(
            contract_version=int(payload["contract_version"]),
            scorer_version=int(payload.get("scorer_version", 0)),
            project_id=str(payload["project_id"]),
            staging_content_hash=str(payload["staging_content_hash"]),
            policy_hash=str(payload["policy_hash"]),
            compared_pair_count=int(payload.get("compared_pair_count", 0)),
            candidates=tuple(
                ResolutionCandidate.from_dict(item)
                for item in payload.get("candidates", ())
            ),
            findings=tuple(
                ResolutionFinding.from_dict(item)
                for item in payload.get("findings", ())
            ),
        )
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Resolution-evaluation content hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class ResolutionDecision:
    decision_id: str
    evaluation_hash: str
    group_id: str
    kind: ResolutionDecisionKind
    row_ids: tuple[str, ...]
    reason: str
    actor: ActorIdentity
    decided_at: datetime
    lifecycle_version: int
    field: str | None = None
    selected_row_id: str | None = None
    replacement_value: Any = None
    contract_version: int = RESOLUTION_DECISION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _uuid(self.decision_id, "resolution decision ID")
        _hash(self.evaluation_hash, "resolution evaluation hash")
        _hash(self.group_id, "resolution group ID")
        object.__setattr__(self, "kind", ResolutionDecisionKind(self.kind))
        if self.contract_version != RESOLUTION_DECISION_CONTRACT_VERSION:
            raise ValueError("Resolution-decision contract version is unsupported")
        if self.lifecycle_version < 1:
            raise ValueError("Resolution decision lifecycle version must be positive")
        if self.decided_at.tzinfo is None or self.decided_at.utcoffset() is None:
            raise ValueError("Resolution decision time must be timezone-aware")
        reason = _required_text(self.reason, "resolution decision reason", 1_000)
        object.__setattr__(self, "reason", reason)
        if len(self.row_ids) < 1 or self.row_ids != tuple(sorted(set(self.row_ids))):
            raise ValueError("Resolution decision rows must be unique and ordered")
        for row_id in self.row_ids:
            _hash(row_id, "resolution decision row ID")
        field_decision = self.kind in {
            ResolutionDecisionKind.SELECT_SOURCE,
            ResolutionDecisionKind.REVIEWER_CORRECTION,
        }
        if field_decision != (self.field is not None):
            raise ValueError("Field decisions require one field")
        if self.field is not None:
            _field(self.field)
        if self.kind is ResolutionDecisionKind.SELECT_SOURCE:
            if self.selected_row_id not in self.row_ids:
                raise ValueError("Selected survivor source is not in the decision group")
            if self.replacement_value is not None:
                raise ValueError("Source selections cannot contain replacement values")
        elif self.kind is ResolutionDecisionKind.REVIEWER_CORRECTION:
            if self.selected_row_id is not None:
                raise ValueError("Reviewer corrections cannot select a source row")
            assert_no_numeric_odoo_ids(portable_value(self.replacement_value))
        elif self.selected_row_id is not None or self.replacement_value is not None:
            raise ValueError("Pair decisions cannot contain field values")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_portable_dict(include_hash=False))

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "decision_id": self.decision_id,
            "evaluation_hash": self.evaluation_hash,
            "group_id": self.group_id,
            "kind": self.kind.value,
            "row_ids": list(self.row_ids),
            "field": self.field,
            "selected_row_id": self.selected_row_id,
            "replacement_value": portable_value(self.replacement_value),
            "reason": self.reason,
            "actor": {
                "issuer": self.actor.issuer,
                "subject_id": self.actor.subject_id,
                "display_name": self.actor.display_name,
            },
            "decided_at": self.decided_at.isoformat(),
            "lifecycle_version": self.lifecycle_version,
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        assert_no_numeric_odoo_ids(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolutionDecision":
        actor = dict(payload["actor"])
        result = cls(
            contract_version=int(payload["contract_version"]),
            decision_id=str(payload["decision_id"]),
            evaluation_hash=str(payload["evaluation_hash"]),
            group_id=str(payload["group_id"]),
            kind=ResolutionDecisionKind(str(payload["kind"])),
            row_ids=tuple(str(item) for item in payload.get("row_ids", ())),
            field=(str(payload["field"]) if payload.get("field") is not None else None),
            selected_row_id=(
                str(payload["selected_row_id"])
                if payload.get("selected_row_id") is not None
                else None
            ),
            replacement_value=restore_portable_value(payload.get("replacement_value")),
            reason=str(payload["reason"]),
            actor=ActorIdentity(
                issuer=str(actor["issuer"]),
                subject_id=str(actor["subject_id"]),
                display_name=str(actor["display_name"]),
            ),
            decided_at=datetime.fromisoformat(str(payload["decided_at"])),
            lifecycle_version=int(payload["lifecycle_version"]),
        )
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Resolution-decision content hash is invalid")
        return result


@dataclass(frozen=True, slots=True)
class FieldProvenance:
    field: str
    kind: FieldProvenanceKind
    result_value_hash: str
    source_row_ids: tuple[str, ...]
    source_field_hashes: tuple[str, ...]
    decision_id: str | None = None

    def __post_init__(self) -> None:
        _field(self.field)
        object.__setattr__(self, "kind", FieldProvenanceKind(self.kind))
        _hash(self.result_value_hash, "field result hash")
        if not self.source_row_ids or self.source_row_ids != tuple(
            sorted(set(self.source_row_ids))
        ):
            raise ValueError("Field provenance requires ordered source rows")
        for row_id in self.source_row_ids:
            _hash(row_id, "field provenance row ID")
        if not self.source_field_hashes:
            raise ValueError("Field provenance requires source field hashes")
        for field_hash in self.source_field_hashes:
            _hash(field_hash, "source field hash")
        decision_required = self.kind in {
            FieldProvenanceKind.SELECTED_SOURCE,
            FieldProvenanceKind.REVIEWER_CORRECTION,
        }
        if decision_required != (self.decision_id is not None):
            raise ValueError("Reviewed field provenance requires a decision")
        if self.decision_id is not None:
            _uuid(self.decision_id, "field provenance decision ID")

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "kind": self.kind.value,
            "result_value_hash": self.result_value_hash,
            "source_row_ids": list(self.source_row_ids),
            "source_field_hashes": list(self.source_field_hashes),
            "decision_id": self.decision_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FieldProvenance":
        return cls(
            field=str(payload["field"]),
            kind=FieldProvenanceKind(str(payload["kind"])),
            result_value_hash=str(payload["result_value_hash"]),
            source_row_ids=tuple(str(item) for item in payload.get("source_row_ids", ())),
            source_field_hashes=tuple(
                str(item) for item in payload.get("source_field_hashes", ())
            ),
            decision_id=(
                str(payload["decision_id"])
                if payload.get("decision_id") is not None
                else None
            ),
        )


@dataclass(frozen=True, slots=True)
class EffectiveRow:
    """Canonical-shaped post-resolution row plus field-level provenance."""

    canonical_row: CanonicalRow
    contributing_row_ids: tuple[str, ...]
    state: ResolutionState
    field_provenance: tuple[FieldProvenance, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", ResolutionState(self.state))
        if not self.contributing_row_ids or self.contributing_row_ids != tuple(
            sorted(set(self.contributing_row_ids))
        ):
            raise ValueError("Effective rows require ordered contributing rows")
        for row_id in self.contributing_row_ids:
            _hash(row_id, "effective contributing row ID")
        expected = tuple(sorted(self.field_provenance, key=lambda item: item.field))
        if self.field_provenance != expected:
            raise ValueError("Effective field provenance must use field order")
        if {item.field for item in self.field_provenance} != set(
            self.canonical_row.proposed_values
        ):
            raise ValueError("Every effective scalar value requires provenance")
        for item in self.field_provenance:
            expected_hash = content_hash(
                portable_value(self.canonical_row.proposed_values[item.field])
            )
            if item.result_value_hash != expected_hash:
                raise ValueError("Effective field provenance does not match its value")

    @property
    def row_id(self) -> str:
        return self.canonical_row.row_id

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "canonical_row": self.canonical_row.to_portable_dict(),
            "contributing_row_ids": list(self.contributing_row_ids),
            "state": self.state.value,
            "field_provenance": [
                item.to_portable_dict() for item in self.field_provenance
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EffectiveRow":
        return cls(
            canonical_row=CanonicalRow.from_dict(dict(payload["canonical_row"])),
            contributing_row_ids=tuple(
                str(item) for item in payload.get("contributing_row_ids", ())
            ),
            state=ResolutionState(str(payload["state"])),
            field_provenance=tuple(
                FieldProvenance.from_dict(item)
                for item in payload.get("field_provenance", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class ResolutionRowAccounting:
    source_row_id: str
    state: ResolutionState
    effective_row_id: str

    def __post_init__(self) -> None:
        _hash(self.source_row_id, "resolution source row ID")
        _hash(self.effective_row_id, "resolution effective row ID")
        object.__setattr__(self, "state", ResolutionState(self.state))

    def to_portable_dict(self) -> dict[str, str]:
        return {
            "source_row_id": self.source_row_id,
            "state": self.state.value,
            "effective_row_id": self.effective_row_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolutionRowAccounting":
        return cls(
            source_row_id=str(payload["source_row_id"]),
            state=ResolutionState(str(payload["state"])),
            effective_row_id=str(payload["effective_row_id"]),
        )


@dataclass(frozen=True, slots=True)
class ResolutionReconciliation:
    staged_rows: int
    passed_through_rows: int
    kept_distinct_rows: int
    merged_input_rows: int
    survivor_rows: int
    corrected_effective_rows: int
    effective_rows: int

    def __post_init__(self) -> None:
        values = (
            self.staged_rows,
            self.passed_through_rows,
            self.kept_distinct_rows,
            self.merged_input_rows,
            self.survivor_rows,
            self.corrected_effective_rows,
            self.effective_rows,
        )
        if any(item < 0 for item in values):
            raise ValueError("Resolution reconciliation counts cannot be negative")
        if self.staged_rows != (
            self.passed_through_rows
            + self.kept_distinct_rows
            + self.merged_input_rows
        ):
            raise ValueError("Resolution reconciliation does not account for staged rows")
        if self.effective_rows != (
            self.passed_through_rows + self.kept_distinct_rows + self.survivor_rows
        ):
            raise ValueError("Resolution reconciliation does not account for effective rows")
        if self.survivor_rows and self.merged_input_rows < self.survivor_rows * 2:
            raise ValueError("Every survivor requires at least two input rows")
        if self.corrected_effective_rows > self.effective_rows:
            raise ValueError("Corrected rows cannot exceed effective rows")

    def to_portable_dict(self) -> dict[str, int]:
        return {
            "staged_rows": self.staged_rows,
            "passed_through_rows": self.passed_through_rows,
            "kept_distinct_rows": self.kept_distinct_rows,
            "merged_input_rows": self.merged_input_rows,
            "survivor_rows": self.survivor_rows,
            "corrected_effective_rows": self.corrected_effective_rows,
            "effective_rows": self.effective_rows,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolutionReconciliation":
        return cls(**{field: int(payload[field]) for field in (
            "staged_rows",
            "passed_through_rows",
            "kept_distinct_rows",
            "merged_input_rows",
            "survivor_rows",
            "corrected_effective_rows",
            "effective_rows",
        )})


@dataclass(frozen=True, slots=True)
class EffectiveDataset:
    """Immutable exact post-resolution input for quality evaluation."""

    project_id: str
    staging_content_hash: str
    policy_hash: str
    evaluation_hash: str
    decisions_hash: str
    rows: tuple[EffectiveRow, ...]
    accounting: tuple[ResolutionRowAccounting, ...]
    reconciliation: ResolutionReconciliation
    contract_version: int = EFFECTIVE_DATASET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _required_text(self.project_id, "project ID", 200)
        for value, label in (
            (self.staging_content_hash, "staging content hash"),
            (self.policy_hash, "resolution policy hash"),
            (self.evaluation_hash, "resolution evaluation hash"),
            (self.decisions_hash, "resolution decisions hash"),
        ):
            _hash(value, label)
        if self.contract_version != EFFECTIVE_DATASET_CONTRACT_VERSION:
            raise ValueError("Effective-dataset contract version is unsupported")
        if self.rows != tuple(sorted(self.rows, key=lambda item: item.row_id)):
            raise ValueError("Effective rows must use deterministic order")
        if len({item.row_id for item in self.rows}) != len(self.rows):
            raise ValueError("Effective row IDs must be unique")
        if self.accounting != tuple(
            sorted(self.accounting, key=lambda item: item.source_row_id)
        ):
            raise ValueError("Resolution accounting must use source-row order")
        if len({item.source_row_id for item in self.accounting}) != len(self.accounting):
            raise ValueError("Every staged row requires one resolution state")
        effective_ids = {item.row_id for item in self.rows}
        if any(item.effective_row_id not in effective_ids for item in self.accounting):
            raise ValueError("Resolution accounting references a missing effective row")
        if self.reconciliation.staged_rows != len(self.accounting):
            raise ValueError("Resolution accounting count is inconsistent")
        if self.reconciliation.effective_rows != len(self.rows):
            raise ValueError("Effective-row count is inconsistent")

    @property
    def content_hash(self) -> str:
        return content_hash(self.to_portable_dict(include_hash=False))

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": self.contract_version,
            "project_id": self.project_id,
            "staging_content_hash": self.staging_content_hash,
            "policy_hash": self.policy_hash,
            "evaluation_hash": self.evaluation_hash,
            "decisions_hash": self.decisions_hash,
            "rows": [item.to_portable_dict() for item in self.rows],
            "accounting": [item.to_portable_dict() for item in self.accounting],
            "reconciliation": self.reconciliation.to_portable_dict(),
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        assert_no_numeric_odoo_ids(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "EffectiveDataset":
        result = cls(
            contract_version=int(payload["contract_version"]),
            project_id=str(payload["project_id"]),
            staging_content_hash=str(payload["staging_content_hash"]),
            policy_hash=str(payload["policy_hash"]),
            evaluation_hash=str(payload["evaluation_hash"]),
            decisions_hash=str(payload["decisions_hash"]),
            rows=tuple(EffectiveRow.from_dict(item) for item in payload.get("rows", ())),
            accounting=tuple(
                ResolutionRowAccounting.from_dict(item)
                for item in payload.get("accounting", ())
            ),
            reconciliation=ResolutionReconciliation.from_dict(
                dict(payload["reconciliation"])
            ),
        )
        if payload.get("content_hash") != result.content_hash:
            raise ValueError("Effective-dataset content hash is invalid")
        return result


def build_effective_dataset(
    *,
    policy: ResolutionPolicy,
    evaluation: ResolutionEvaluation,
    rows: Iterable[CanonicalRow],
    decisions: Iterable[ResolutionDecision],
) -> EffectiveDataset:
    """Apply complete reviewed decisions and publish immutable effective rows.

    The function is pure and fail-closed. Candidate scores never enter the
    merge decision; only explicit ``SAME_RECORD`` decisions create survivor
    components. Every candidate requires a reviewed pair decision.
    """

    if evaluation.project_id != policy.project_id or evaluation.policy_hash != policy.content_hash:
        raise ValueError("Resolution evaluation does not match its policy")
    if evaluation.blocked:
        raise ValueError("Blocking resolution findings must be corrected before review")
    source_rows = tuple(sorted(rows, key=lambda item: item.row_id))
    source_by_id = {item.row_id: item for item in source_rows}
    if len(source_by_id) != len(source_rows):
        raise ValueError("Canonical resolution input contains duplicate row IDs")
    reviewed = tuple(sorted(decisions, key=lambda item: item.decision_id))
    if any(item.evaluation_hash != evaluation.content_hash for item in reviewed):
        raise ValueError("Resolution decision belongs to another evaluation")
    if len({item.decision_id for item in reviewed}) != len(reviewed):
        raise ValueError("Resolution decision IDs must be unique")
    if any(row_id not in source_by_id for item in reviewed for row_id in item.row_ids):
        raise ValueError("Resolution decision references an unknown canonical row")

    candidate_pairs = {
        (item.left_row_id, item.right_row_id): item for item in evaluation.candidates
    }
    pair_decisions: dict[tuple[str, str], ResolutionDecision] = {}
    field_decisions: dict[tuple[str, str], ResolutionDecision] = {}
    for decision in reviewed:
        if decision.kind in {
            ResolutionDecisionKind.SAME_RECORD,
            ResolutionDecisionKind.KEEP_SEPARATE,
        }:
            if len(decision.row_ids) != 2:
                raise ValueError("Candidate pair decisions require exactly two rows")
            pair = (decision.row_ids[0], decision.row_ids[1])
            if pair not in candidate_pairs:
                raise ValueError("Pair decision does not match a generated candidate")
            if pair in pair_decisions:
                raise ValueError("Candidate pair was decided more than once")
            pair_decisions[pair] = decision
        else:
            if decision.field is None:
                raise ValueError("Survivor field decision is incomplete")
            key = (decision.group_id, decision.field)
            if key in field_decisions:
                raise ValueError("Survivor field was decided more than once")
            field_decisions[key] = decision
    if set(pair_decisions) != set(candidate_pairs):
        raise ValueError("Every fuzzy candidate requires a reviewed decision")

    parent = {row_id: row_id for row_id in source_by_id}

    def find(row_id: str) -> str:
        while parent[row_id] != row_id:
            parent[row_id] = parent[parent[row_id]]
            row_id = parent[row_id]
        return row_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        parent[second] = first

    for pair, decision in pair_decisions.items():
        if decision.kind is ResolutionDecisionKind.SAME_RECORD:
            union(*pair)
    for pair, decision in pair_decisions.items():
        if decision.kind is ResolutionDecisionKind.KEEP_SEPARATE and find(pair[0]) == find(pair[1]):
            raise ValueError("Keep-separate decision conflicts with an accepted merge group")

    groups: dict[str, list[str]] = {}
    for row_id in source_by_id:
        groups.setdefault(find(row_id), []).append(row_id)
    merge_groups = {
        _group_id(policy.content_hash, tuple(sorted(group_rows))): tuple(sorted(group_rows))
        for group_rows in groups.values()
        if len(group_rows) > 1
    }
    valid_field_group_ids = set(merge_groups)
    valid_field_group_ids.update(
        _group_id(policy.content_hash, (row_id,)) for row_id in source_by_id
    )
    if any(group_id not in valid_field_group_ids for group_id, _ in field_decisions):
        raise ValueError("Survivor field decision does not match a resolution group")
    candidate_rows = {
        row_id for pair in candidate_pairs for row_id in pair
    }

    effective_rows: list[EffectiveRow] = []
    accounting: list[ResolutionRowAccounting] = []
    corrected_effective_rows = 0
    merged_source_rows: set[str] = set()
    for group_id, group_rows in sorted(merge_groups.items()):
        survivor, corrected = _survivor(
            group_id,
            group_rows,
            source_by_id,
            policy,
            field_decisions,
        )
        effective_rows.append(survivor)
        corrected_effective_rows += int(corrected)
        merged_source_rows.update(group_rows)
        accounting.extend(
            ResolutionRowAccounting(
                source_row_id=row_id,
                state=ResolutionState.CONTRIBUTED_TO_SURVIVOR,
                effective_row_id=survivor.row_id,
            )
            for row_id in group_rows
        )

    for row in source_rows:
        if row.row_id in merged_source_rows:
            continue
        group_id = _group_id(policy.content_hash, (row.row_id,))
        corrected_row = _single_row_with_corrections(
            row,
            group_id,
            policy,
            field_decisions,
        )
        effective = corrected_row or pass_through_effective_row(row)
        if corrected_row is not None:
            corrected_effective_rows += 1
        state = (
            ResolutionState.KEPT_DISTINCT
            if row.row_id in candidate_rows
            else ResolutionState.PASSED_THROUGH
        )
        if state is ResolutionState.KEPT_DISTINCT:
            effective = EffectiveRow(
                canonical_row=effective.canonical_row,
                contributing_row_ids=effective.contributing_row_ids,
                state=state,
                field_provenance=effective.field_provenance,
            )
        effective_rows.append(effective)
        accounting.append(
            ResolutionRowAccounting(
                source_row_id=row.row_id,
                state=state,
                effective_row_id=effective.row_id,
            )
        )

    decisions_hash = content_hash(
        [item.to_portable_dict() for item in reviewed]
    )
    passed = sum(item.state is ResolutionState.PASSED_THROUGH for item in accounting)
    kept = sum(item.state is ResolutionState.KEPT_DISTINCT for item in accounting)
    reconciliation = ResolutionReconciliation(
        staged_rows=len(source_rows),
        passed_through_rows=passed,
        kept_distinct_rows=kept,
        merged_input_rows=len(merged_source_rows),
        survivor_rows=len(merge_groups),
        corrected_effective_rows=corrected_effective_rows,
        effective_rows=len(effective_rows),
    )
    return EffectiveDataset(
        project_id=policy.project_id,
        staging_content_hash=evaluation.staging_content_hash,
        policy_hash=policy.content_hash,
        evaluation_hash=evaluation.content_hash,
        decisions_hash=decisions_hash,
        rows=tuple(sorted(effective_rows, key=lambda item: item.row_id)),
        accounting=tuple(sorted(accounting, key=lambda item: item.source_row_id)),
        reconciliation=reconciliation,
    )


def evaluate_resolution_candidates(
    *,
    policy: ResolutionPolicy,
    staging_content_hash: str,
    rows: Iterable[CanonicalRow],
) -> ResolutionEvaluation:
    """Generate bounded deterministic candidates without changing any row."""

    _hash(staging_content_hash, "staging content hash")
    rules = {item.dataset: item for item in policy.rules}
    rows_by_dataset: dict[str, list[CanonicalRow]] = {}
    for row in rows:
        if row.dataset in rules and row.disposition in {
            StagingDisposition.CANDIDATE,
            StagingDisposition.REFERENCE,
        }:
            rows_by_dataset.setdefault(row.dataset, []).append(row)

    raw_candidates: list[ResolutionCandidate] = []
    findings: list[ResolutionFinding] = []
    compared_pair_count = 0
    for dataset, dataset_rows in sorted(rows_by_dataset.items()):
        rule = rules[dataset]
        blocks: dict[tuple[str, ...], list[CanonicalRow]] = {}
        missing_count = 0
        for row in sorted(dataset_rows, key=lambda item: item.row_id):
            block_key = tuple(
                _normalized_text(row.proposed_values.get(field), rule.punctuation)
                for field in rule.blocking_fields
            )
            if any(not item for item in block_key):
                missing_count += 1
                continue
            blocks.setdefault(block_key, []).append(row)
        if missing_count:
            findings.append(
                _finding(
                    rule,
                    "FUZZY_BLOCKING_VALUE_MISSING",
                    "Possible duplicates could not be checked because a blocking value is blank.",
                    missing_count,
                )
            )
        for block_key, block_rows in sorted(blocks.items()):
            if len(block_rows) > rule.max_block_size:
                findings.append(
                    _finding(
                        rule,
                        "FUZZY_BLOCK_TOO_LARGE",
                        "Possible-duplicate group is too broad; choose a more selective blocking field.",
                        len(block_rows),
                        discriminator=block_key,
                    )
                )
                continue
            for left_index, left in enumerate(block_rows):
                for right in block_rows[left_index + 1 :]:
                    compared_pair_count += 1
                    candidate = _candidate(rule, block_key, left, right)
                    if Decimal(candidate.score) >= Decimal(rule.candidate_threshold):
                        raw_candidates.append(candidate)

    retained = _bounded_candidates(raw_candidates, rules)
    return ResolutionEvaluation(
        project_id=policy.project_id,
        staging_content_hash=staging_content_hash,
        policy_hash=policy.content_hash,
        candidates=tuple(sorted(retained, key=lambda item: item.candidate_id)),
        findings=tuple(sorted(findings, key=lambda item: item.finding_id)),
        compared_pair_count=compared_pair_count,
    )


def pass_through_effective_row(row: CanonicalRow) -> EffectiveRow:
    """Create deterministic pass-through provenance for a canonical row."""

    provenance = tuple(
        FieldProvenance(
            field=field,
            kind=FieldProvenanceKind.COPIED,
            result_value_hash=content_hash(portable_value(value)),
            source_row_ids=(row.row_id,),
            source_field_hashes=(content_hash(portable_value(value)),),
        )
        for field, value in sorted(row.proposed_values.items())
    )
    return EffectiveRow(
        canonical_row=row,
        contributing_row_ids=(row.row_id,),
        state=ResolutionState.PASSED_THROUGH,
        field_provenance=provenance,
    )


def resolution_group_id(policy_hash: str, row_ids: Iterable[str]) -> str:
    """Return the stable survivor-group ID used by field decisions."""

    ordered = tuple(sorted(set(row_ids)))
    if not ordered:
        raise ValueError("Resolution groups require at least one row")
    for row_id in ordered:
        _hash(row_id, "resolution group row ID")
    return _group_id(policy_hash, ordered)


def _survivor(
    group_id: str,
    group_rows: tuple[str, ...],
    source_by_id: Mapping[str, CanonicalRow],
    policy: ResolutionPolicy,
    field_decisions: Mapping[tuple[str, str], ResolutionDecision],
) -> tuple[EffectiveRow, bool]:
    rows = tuple(source_by_id[row_id] for row_id in group_rows)
    datasets = {item.dataset for item in rows}
    models = {item.target_model for item in rows}
    dispositions = {item.disposition for item in rows}
    if len(datasets) != 1 or len(models) != 1 or len(dispositions) != 1:
        raise ValueError("A survivor group must use one dataset, model, and disposition")
    dataset = rows[0].dataset
    rule = next((item for item in policy.rules if item.dataset == dataset), None)
    if rule is None:
        raise ValueError("Survivor group has no resolution rule")
    fields = set(rows[0].proposed_values)
    if any(set(item.proposed_values) != fields for item in rows):
        raise ValueError("Survivor rows have incompatible scalar shapes")
    if fields != set(rule.survivor_fields):
        raise ValueError("Resolution rule must govern every survivor scalar field")

    proposed: dict[str, Any] = {}
    provenance: list[FieldProvenance] = []
    corrected = False
    for field in sorted(fields):
        values = tuple(item.proposed_values[field] for item in rows)
        value_hashes = tuple(content_hash(portable_value(item)) for item in values)
        unique_hashes = set(value_hashes)
        decision = field_decisions.get((group_id, field))
        if len(unique_hashes) == 1:
            if decision is not None:
                raise ValueError("Unanimous survivor fields cannot receive a decision")
            proposed[field] = values[0]
            provenance.append(
                FieldProvenance(
                    field=field,
                    kind=FieldProvenanceKind.UNANIMOUS,
                    result_value_hash=value_hashes[0],
                    source_row_ids=group_rows,
                    source_field_hashes=tuple(sorted(value_hashes)),
                )
            )
            continue
        if decision is None:
            raise ValueError(f"Survivor field {field!r} still requires a decision")
        if decision.row_ids != group_rows:
            raise ValueError("Survivor field decision rows do not match the group")
        if decision.kind is ResolutionDecisionKind.SELECT_SOURCE:
            selected = source_by_id[decision.selected_row_id or ""]
            value = selected.proposed_values[field]
            kind = FieldProvenanceKind.SELECTED_SOURCE
            source_hashes = (content_hash(portable_value(value)),)
        elif decision.kind is ResolutionDecisionKind.REVIEWER_CORRECTION:
            if field not in rule.correctable_fields:
                raise ValueError("Reviewer correction is not allowed for this field")
            value = decision.replacement_value
            _require_same_scalar_type(values, value)
            kind = FieldProvenanceKind.REVIEWER_CORRECTION
            source_hashes = tuple(sorted(value_hashes))
            corrected = True
        else:
            raise ValueError("Survivor field decision kind is unsupported")
        proposed[field] = value
        provenance.append(
            FieldProvenance(
                field=field,
                kind=kind,
                result_value_hash=content_hash(portable_value(value)),
                source_row_ids=group_rows,
                source_field_hashes=source_hashes,
                decision_id=decision.decision_id,
            )
        )

    identity_source = _selected_structural_source(
        group_id,
        "__identity__",
        group_rows,
        rows,
        field_decisions,
        value_getter=lambda item: (item.source_identity, item.target_identity),
    )
    scope_source = _selected_structural_source(
        group_id,
        "__scope__",
        group_rows,
        rows,
        field_decisions,
        value_getter=lambda item: item.target_scope,
    )
    reference_source = _selected_structural_source(
        group_id,
        "__references__",
        group_rows,
        rows,
        field_decisions,
        value_getter=lambda item: item.references,
    )
    physical_sources = {
        dataset_id: tuple(
            sorted(
                {
                    source_row
                    for row in rows
                    for source_row in row.lineage.physical_sources.get(dataset_id, ())
                }
            )
        )
        for dataset_id in sorted(
            {
                dataset_id
                for row in rows
                for dataset_id in row.lineage.physical_sources
            }
        )
    }
    primary_dataset_id = next(iter(physical_sources))
    physical_rows = physical_sources[primary_dataset_id]
    field_sources: dict[str, tuple[str, ...]] = {}
    for field in fields:
        field_sources[field] = tuple(
            sorted(
                {
                    source
                    for row in rows
                    for source in row.lineage.field_sources.get(field, ())
                }
            )
        )
    issues_by_hash = {
        content_hash(item.to_portable_dict()): item
        for row in rows
        for item in row.issues
    }
    row_payload = {
        "group_id": group_id,
        "rows": group_rows,
        "values": portable_value(proposed),
        "identity_source": identity_source.row_id,
        "scope_source": scope_source.row_id,
        "reference_source": reference_source.row_id,
    }
    survivor_row = CanonicalRow(
        row_id=content_hash(row_payload),
        dataset=dataset,
        source_row=min(item.source_row for item in rows),
        target_model=rows[0].target_model,
        disposition=rows[0].disposition,
        source_identity=identity_source.source_identity,
        target_identity=identity_source.target_identity,
        target_scope=scope_source.target_scope,
        proposed_values=proposed,
        references=reference_source.references,
        issues=tuple(item for _, item in sorted(issues_by_hash.items())),
        lineage=CanonicalLineage(
            source_selection_hash=rows[0].lineage.source_selection_hash,
            source_hash=rows[0].lineage.source_hash,
            mapping_hash=rows[0].lineage.mapping_hash,
            schema_hash=rows[0].lineage.schema_hash,
            derived_plan_hash=rows[0].lineage.derived_plan_hash,
            dataset=dataset,
            source_row=min(item.source_row for item in rows),
            physical_dataset_id=primary_dataset_id,
            physical_source_rows=physical_rows,
            field_sources=field_sources,
            physical_sources=physical_sources,
        ),
    )
    return (
        EffectiveRow(
            canonical_row=survivor_row,
            contributing_row_ids=group_rows,
            state=ResolutionState.CONTRIBUTED_TO_SURVIVOR,
            field_provenance=tuple(provenance),
        ),
        corrected,
    )


def _selected_structural_source(
    group_id: str,
    field: str,
    group_rows: tuple[str, ...],
    rows: tuple[CanonicalRow, ...],
    field_decisions: Mapping[tuple[str, str], ResolutionDecision],
    *,
    value_getter: Any,
) -> CanonicalRow:
    value_hashes = {
        content_hash(portable_value(value_getter(item))) for item in rows
    }
    decision = field_decisions.get((group_id, field))
    if len(value_hashes) == 1:
        if decision is not None:
            raise ValueError("Unanimous survivor structure cannot receive a decision")
        return rows[0]
    if decision is None or decision.kind is not ResolutionDecisionKind.SELECT_SOURCE:
        raise ValueError(f"Survivor {field} still requires a source decision")
    if decision.row_ids != group_rows:
        raise ValueError("Survivor structure decision rows do not match the group")
    return next(item for item in rows if item.row_id == decision.selected_row_id)


def _single_row_with_corrections(
    row: CanonicalRow,
    group_id: str,
    policy: ResolutionPolicy,
    field_decisions: Mapping[tuple[str, str], ResolutionDecision],
) -> EffectiveRow | None:
    rule = next((item for item in policy.rules if item.dataset == row.dataset), None)
    if rule is None:
        return None
    corrections = tuple(
        decision
        for (decision_group, _), decision in field_decisions.items()
        if decision_group == group_id
    )
    if not corrections:
        return None
    proposed = dict(row.proposed_values)
    provenance = {
        item.field: item for item in pass_through_effective_row(row).field_provenance
    }
    for decision in corrections:
        if decision.kind is not ResolutionDecisionKind.REVIEWER_CORRECTION:
            raise ValueError("Single-row review supports only governed corrections")
        if decision.row_ids != (row.row_id,) or decision.field not in rule.correctable_fields:
            raise ValueError("Single-row correction is outside the approved policy")
        field = decision.field or ""
        _require_same_scalar_type((proposed[field],), decision.replacement_value)
        before_hash = content_hash(portable_value(proposed[field]))
        proposed[field] = decision.replacement_value
        provenance[field] = FieldProvenance(
            field=field,
            kind=FieldProvenanceKind.REVIEWER_CORRECTION,
            result_value_hash=content_hash(portable_value(decision.replacement_value)),
            source_row_ids=(row.row_id,),
            source_field_hashes=(before_hash,),
            decision_id=decision.decision_id,
        )
    corrected = CanonicalRow(
        row_id=content_hash(
            {
                "base_row_id": row.row_id,
                "values": portable_value(proposed),
                "decisions": sorted(item.decision_id for item in corrections),
            }
        ),
        dataset=row.dataset,
        source_row=row.source_row,
        target_model=row.target_model,
        disposition=row.disposition,
        source_identity=row.source_identity,
        target_identity=row.target_identity,
        target_scope=row.target_scope,
        proposed_values=proposed,
        references=row.references,
        issues=row.issues,
        lineage=row.lineage,
    )
    return EffectiveRow(
        canonical_row=corrected,
        contributing_row_ids=(row.row_id,),
        state=ResolutionState.PASSED_THROUGH,
        field_provenance=tuple(sorted(provenance.values(), key=lambda item: item.field)),
    )


def _group_id(policy_hash: str, row_ids: tuple[str, ...]) -> str:
    return content_hash({"policy_hash": policy_hash, "row_ids": list(row_ids)})


def _require_same_scalar_type(source_values: tuple[Any, ...], replacement: Any) -> None:
    """Keep corrections inside the canonical field's existing typed boundary."""

    source_types = {type(value) for value in source_values if value is not None}
    if len(source_types) != 1 or replacement is None:
        raise ValueError("Reviewer correction cannot infer a safe field type")
    expected = next(iter(source_types))
    if type(replacement) is not expected:
        raise ValueError("Reviewer correction must preserve the field value type")
    if isinstance(replacement, Decimal) and not replacement.is_finite():
        raise ValueError("Reviewer correction decimal must be finite")
    if not isinstance(replacement, (str, bool, int, Decimal, date, datetime)):
        raise ValueError("Reviewer correction supports typed scalar values only")


def _candidate(
    rule: ResolutionRule,
    block_key: tuple[str, ...],
    left: CanonicalRow,
    right: CanonicalRow,
) -> ResolutionCandidate:
    components: dict[str, str] = {}
    weighted = Decimal("0")
    total_weight = Decimal("0")
    for comparison in rule.comparison_fields:
        left_value = _normalized_text(
            left.proposed_values.get(comparison.field),
            rule.punctuation,
        )
        right_value = _normalized_text(
            right.proposed_values.get(comparison.field),
            rule.punctuation,
        )
        if comparison.algorithm is SimilarityAlgorithm.NORMALIZED_LEVENSHTEIN:
            component = _levenshtein_score(left_value, right_value)
        else:
            component = _jaccard_score(left_value, right_value)
        weight = Decimal(comparison.weight)
        components[comparison.field] = _score_text(component)
        weighted += component * weight
        total_weight += weight
    score = (weighted / total_weight).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN)
    left_row_id, right_row_id = sorted((left.row_id, right.row_id))
    candidate_payload = {
        "rule_id": rule.rule_id,
        "dataset": rule.dataset,
        "left_row_id": left_row_id,
        "right_row_id": right_row_id,
        "block_key": block_key,
        "score": _score_text(score),
        "components": components,
        "scorer_version": RESOLUTION_SCORER_VERSION,
    }
    return ResolutionCandidate(
        candidate_id=content_hash(candidate_payload),
        rule_id=rule.rule_id,
        dataset=rule.dataset,
        left_row_id=left_row_id,
        right_row_id=right_row_id,
        block_key=block_key,
        score=_score_text(score),
        components=components,
    )


def _bounded_candidates(
    candidates: Iterable[ResolutionCandidate],
    rules: Mapping[str, ResolutionRule],
) -> tuple[ResolutionCandidate, ...]:
    degree: dict[str, int] = {}
    retained: list[ResolutionCandidate] = []
    ordered = sorted(
        candidates,
        key=lambda item: (-Decimal(item.score), item.left_row_id, item.right_row_id),
    )
    for item in ordered:
        limit = rules[item.dataset].max_candidates_per_row
        if degree.get(item.left_row_id, 0) >= limit:
            continue
        if degree.get(item.right_row_id, 0) >= limit:
            continue
        retained.append(item)
        degree[item.left_row_id] = degree.get(item.left_row_id, 0) + 1
        degree[item.right_row_id] = degree.get(item.right_row_id, 0) + 1
    return tuple(retained)


def _finding(
    rule: ResolutionRule,
    code: str,
    message: str,
    affected_count: int,
    *,
    discriminator: tuple[str, ...] = (),
) -> ResolutionFinding:
    payload = {
        "rule_id": rule.rule_id,
        "dataset": rule.dataset,
        "code": code,
        "affected_count": affected_count,
        "discriminator": discriminator,
    }
    return ResolutionFinding(
        finding_id=content_hash(payload),
        rule_id=rule.rule_id,
        dataset=rule.dataset,
        code=code,
        message=message,
        affected_count=affected_count,
    )


def _normalized_text(value: Any, punctuation: str) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    translation = str.maketrans({item: " " for item in punctuation})
    return " ".join(text.translate(translation).split())


def _levenshtein_score(left: str, right: str) -> Decimal:
    if not left or not right:
        return Decimal("0")
    if left == right:
        return Decimal("1")
    previous = list(range(len(right) + 1))
    for left_index, left_character in enumerate(left, 1):
        current = [left_index]
        for right_index, right_character in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    distance = previous[-1]
    return (
        Decimal(1) - (Decimal(distance) / Decimal(max(len(left), len(right))))
    ).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN)


def _jaccard_score(left: str, right: str) -> Decimal:
    left_tokens = frozenset(left.split())
    right_tokens = frozenset(right.split())
    if not left_tokens or not right_tokens:
        return Decimal("0")
    return (
        Decimal(len(left_tokens & right_tokens))
        / Decimal(len(left_tokens | right_tokens))
    ).quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN)


def _ordered_fields(
    fields: tuple[str, ...],
    label: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not fields and not allow_empty:
        raise ValueError(f"Resolution {label} are required")
    for field in fields:
        _field(field)
    expected = tuple(sorted(set(fields)))
    if fields != expected:
        raise ValueError(f"Resolution {label} must be unique and ordered")
    return fields


def _field(value: str) -> None:
    if not _FIELD.fullmatch(value):
        raise ValueError("Resolution field name is invalid")


def _valid_component_name(value: str) -> str:
    _field(value)
    return value


def _decimal(value: str, label: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError) as error:
        raise ValueError(f"{label} must be numeric") from error
    if not result.is_finite():
        raise ValueError(f"{label} must be finite")
    return result


def _bounded_score(value: str, label: str) -> Decimal:
    result = _decimal(value, label)
    if result < 0 or result > 1:
        raise ValueError(f"{label} must be between zero and one")
    return result


def _score_text(value: Decimal) -> str:
    return format(value.quantize(_SCORE_QUANTUM, rounding=ROUND_HALF_EVEN), "f")


def _required_text(value: str, label: str, maximum: int) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{label} is required")
    if len(clean) > maximum:
        raise ValueError(f"{label} is too long")
    return clean


def _hash(value: str, label: str) -> None:
    if not _SHA256.fullmatch(value):
        raise ValueError(f"{label} must be a SHA-256 hash")


def _uuid(value: str, label: str) -> None:
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"{label} is invalid") from error
