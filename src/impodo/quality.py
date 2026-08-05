"""Deterministic, target-independent data-quality and quarantine evidence.

The quality overlay is deliberately separate from canonical staging.  It may
classify or set aside canonical records, but it never rewrites prepared values
or contacts Odoo.  All identifiers in the portable contract are business-side
hashes; numeric Odoo identifiers are forbidden by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from .models import LogicalReference, PreparedRecord, canonical_json_bytes, portable_value
from .projects import MigrationProject
from .source import PreparedBundle
from .staging_contracts import CanonicalIssue, CanonicalRow, CanonicalStagingRun, StagingDisposition


QUALITY_CONTRACT_VERSION = 1
QUALITY_EVALUATOR_VERSION = 1
QUALITY_RULESET_CONTRACT_VERSION = 1
MAX_MANAGER_RULES_PER_DATASET = 3
MANDATORY_QUALITY_FAMILIES = (
    "REQUIRED_VALUES",
    "BOUNDED_VALUES",
    "GOVERNED_LOOKUPS",
    "RELATIONSHIP_READINESS",
    "IDENTITY_COLLISION",
)


class QualityError(ValueError):
    """Raised when quality evidence cannot be evaluated or reconciled safely."""


class QualityRuleFamily(StrEnum):
    REQUIRED_VALUES = "REQUIRED_VALUES"
    BOUNDED_VALUES = "BOUNDED_VALUES"
    GOVERNED_LOOKUPS = "GOVERNED_LOOKUPS"
    RELATIONSHIP_READINESS = "RELATIONSHIP_READINESS"
    IDENTITY_COLLISION = "IDENTITY_COLLISION"
    REQUIRED_IF = "REQUIRED_IF"
    EXACTLY_ONE_OF = "EXACTLY_ONE_OF"
    ORDERED_COMPARISON = "ORDERED_COMPARISON"
    EQUALITY = "EQUALITY"
    INEQUALITY = "INEQUALITY"


class QualityOutcomePolicy(StrEnum):
    WARNING = "WARNING"
    BLOCK = "BLOCK"
    QUARANTINE = "QUARANTINE"
    EXCLUDE = "EXCLUDE"


class QualityRuleSource(StrEnum):
    MAPPING_DERIVED = "MAPPING_DERIVED"
    SCHEMA_DERIVED = "SCHEMA_DERIVED"
    MANAGER_AUTHORED = "MANAGER_AUTHORED"


class QualityOwnerRole(StrEnum):
    DATA_MANAGER = "DATA_MANAGER"
    FUNCTIONAL_OWNER = "FUNCTIONAL_OWNER"


class QualityDisposition(StrEnum):
    CANDIDATE = "CANDIDATE"
    REFERENCE = "REFERENCE"
    BLOCKED = "BLOCKED"
    QUARANTINED = "QUARANTINED"
    EXCLUDED = "EXCLUDED"


class SourceAccountingState(StrEnum):
    REPRESENTED = "REPRESENTED"
    QUARANTINED_BEFORE_TRANSFORM = "QUARANTINED_BEFORE_TRANSFORM"
    EXCLUDED_BY_RULE = "EXCLUDED_BY_RULE"
    UNREPRESENTED = "UNREPRESENTED"


class QualityRunStatus(StrEnum):
    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, slots=True)
class QualityRule:
    rule_id: str
    dataset: str
    family: QualityRuleFamily
    name: str
    explanation: str
    input_fields: tuple[str, ...]
    parameters: Mapping[str, str]
    outcome: QualityOutcomePolicy
    owner_role: QualityOwnerRole
    source: QualityRuleSource
    review_by_days: int | None = None
    evidence_display: str = "masked"

    def __post_init__(self) -> None:
        _require_hash(self.rule_id, "quality rule ID")
        if not self.dataset or not self.name or not self.explanation:
            raise ValueError("Quality rules require a table, name and explanation")
        if len(self.input_fields) > 3 or len(set(self.input_fields)) != len(
            self.input_fields
        ):
            raise ValueError("Quality rules support up to three unique fields")
        if any(not item for item in self.input_fields):
            raise ValueError("Quality-rule fields cannot be empty")
        if any(not str(key) or len(str(value)) > 200 for key, value in self.parameters.items()):
            raise ValueError("Quality-rule parameters are invalid")
        if self.review_by_days is not None and not 1 <= self.review_by_days <= 3650:
            raise ValueError("Quality review timing must be between 1 and 3650 days")
        if self.evidence_display not in {"masked", "plain"}:
            raise ValueError("Quality evidence display policy is unsupported")
        if (
            self.source is not QualityRuleSource.MANAGER_AUTHORED
            and self.family.value not in MANDATORY_QUALITY_FAMILIES
        ):
            raise ValueError("Derived quality rules must use an automatic family")
        if self.outcome is QualityOutcomePolicy.EXCLUDE and self.source is not QualityRuleSource.MANAGER_AUTHORED:
            raise ValueError("Only an explicit manager rule may exclude records")
        if self.source is QualityRuleSource.MANAGER_AUTHORED:
            if self.family.value in MANDATORY_QUALITY_FAMILIES:
                raise ValueError("Manager rules must use a guided business family")
            if len(self.input_fields) != 2:
                raise ValueError("Guided business checks require two fields")
            allowed_parameters = (
                {"equals"}
                if self.family is QualityRuleFamily.REQUIRED_IF
                else set()
            )
            if set(self.parameters) != allowed_parameters:
                raise ValueError("Guided business-check parameters are invalid")
            if self.family is QualityRuleFamily.REQUIRED_IF and not self.parameters.get("equals"):
                raise ValueError("Required-if checks need a condition value")

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "dataset": self.dataset,
            "family": self.family.value,
            "name": self.name,
            "explanation": self.explanation,
            "input_fields": list(self.input_fields),
            "parameters": dict(sorted(self.parameters.items())),
            "outcome": self.outcome.value,
            "owner_role": self.owner_role.value,
            "source": self.source.value,
            "review_by_days": self.review_by_days,
            "evidence_display": self.evidence_display,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QualityRule":
        return cls(
            rule_id=str(payload["rule_id"]),
            dataset=str(payload["dataset"]),
            family=QualityRuleFamily(str(payload["family"])),
            name=str(payload["name"]),
            explanation=str(payload["explanation"]),
            input_fields=tuple(str(item) for item in payload.get("input_fields", ())),
            parameters={str(key): str(value) for key, value in dict(payload.get("parameters", {})).items()},
            outcome=QualityOutcomePolicy(str(payload["outcome"])),
            owner_role=QualityOwnerRole(str(payload["owner_role"])),
            source=QualityRuleSource(str(payload["source"])),
            review_by_days=(int(payload["review_by_days"]) if payload.get("review_by_days") is not None else None),
            evidence_display=str(payload.get("evidence_display", "masked")),
        )


@dataclass(frozen=True, slots=True)
class QualityRuleSet:
    ruleset_id: str
    project_id: str
    version: int
    parent_version: int | None
    mapping_hash: str
    schema_hash: str
    rules: tuple[QualityRule, ...]
    contract_version: int = QUALITY_RULESET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != QUALITY_RULESET_CONTRACT_VERSION:
            raise ValueError("Quality-rule contract version is unsupported")
        if not self.ruleset_id or not self.project_id or self.version < 1:
            raise ValueError("Quality-rule set identity is invalid")
        if self.parent_version is not None and self.parent_version >= self.version:
            raise ValueError("Quality-rule parent version is invalid")
        _require_hash(self.mapping_hash, "quality mapping hash")
        _require_hash(self.schema_hash, "quality schema hash")
        expected = tuple(sorted(self.rules, key=lambda item: item.rule_id))
        if self.rules != expected or len({item.rule_id for item in self.rules}) != len(self.rules):
            raise ValueError("Quality rules must be unique and deterministically ordered")
        automatic = [
            (item.dataset, item.family)
            for item in self.rules
            if item.source is not QualityRuleSource.MANAGER_AUTHORED
        ]
        if len(set(automatic)) != len(automatic):
            raise ValueError("Automatic quality families must be unique per table")
        manager_counts: dict[str, int] = {}
        for item in self.rules:
            if item.source is QualityRuleSource.MANAGER_AUTHORED:
                manager_counts[item.dataset] = manager_counts.get(item.dataset, 0) + 1
        if any(item > MAX_MANAGER_RULES_PER_DATASET for item in manager_counts.values()):
            raise ValueError("Too many optional business checks were configured")

    @property
    def content_hash(self) -> str:
        return _hash(
            {
                "contract_version": self.contract_version,
                "project_id": self.project_id,
                "mapping_hash": self.mapping_hash,
                "schema_hash": self.schema_hash,
                "rules": [item.to_portable_dict() for item in self.rules],
            }
        )

    @property
    def manager_rules(self) -> tuple[QualityRule, ...]:
        return tuple(item for item in self.rules if item.source is QualityRuleSource.MANAGER_AUTHORED)

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload = {
            "contract_version": self.contract_version,
            "ruleset_id": self.ruleset_id,
            "project_id": self.project_id,
            "version": self.version,
            "parent_version": self.parent_version,
            "mapping_hash": self.mapping_hash,
            "schema_hash": self.schema_hash,
            "rules": [item.to_portable_dict() for item in self.rules],
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json_bytes(self.to_portable_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QualityRuleSet":
        ruleset = cls(
            contract_version=int(payload.get("contract_version", 0)),
            ruleset_id=str(payload["ruleset_id"]),
            project_id=str(payload["project_id"]),
            version=int(payload["version"]),
            parent_version=(int(payload["parent_version"]) if payload.get("parent_version") is not None else None),
            mapping_hash=str(payload["mapping_hash"]),
            schema_hash=str(payload["schema_hash"]),
            rules=tuple(QualityRule.from_dict(item) for item in payload.get("rules", ())),
        )
        if payload.get("content_hash") != ruleset.content_hash:
            raise ValueError("Quality-rule content hash is invalid")
        return ruleset

    @classmethod
    def from_json(cls, value: str) -> "QualityRuleSet":
        return cls.from_dict(json.loads(value))


@dataclass(frozen=True, slots=True)
class QualityIssue:
    issue_id: str
    rule_id: str
    family: QualityRuleFamily
    reason_code: str
    message: str
    dataset: str
    row_id: str | None
    source_row: int | None
    affected_fields: tuple[str, ...]
    policy: QualityOutcomePolicy
    owner_role: QualityOwnerRole
    owner_label: str
    evidence_display: str = "masked"

    def __post_init__(self) -> None:
        _require_hash(self.issue_id, "quality issue ID")
        _require_hash(self.rule_id, "quality issue rule ID")
        if not self.reason_code or not self.message or not self.dataset:
            raise ValueError("Quality issues require a reason, message and table")
        if self.row_id is not None:
            _require_hash(self.row_id, "quality issue row ID")
        if self.source_row is not None and self.source_row < 1:
            raise ValueError("Quality issue source rows must be positive")
        if self.affected_fields != tuple(sorted(set(self.affected_fields))):
            raise ValueError("Quality issue fields must be unique and ordered")

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "rule_id": self.rule_id,
            "family": self.family.value,
            "reason_code": self.reason_code,
            "message": self.message,
            "dataset": self.dataset,
            "row_id": self.row_id,
            "source_row": self.source_row,
            "affected_fields": list(self.affected_fields),
            "policy": self.policy.value,
            "owner_role": self.owner_role.value,
            "owner_label": self.owner_label,
            "evidence_display": self.evidence_display,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QualityIssue":
        return cls(
            issue_id=str(payload["issue_id"]),
            rule_id=str(payload["rule_id"]),
            family=QualityRuleFamily(str(payload["family"])),
            reason_code=str(payload["reason_code"]),
            message=str(payload["message"]),
            dataset=str(payload["dataset"]),
            row_id=(str(payload["row_id"]) if payload.get("row_id") else None),
            source_row=(int(payload["source_row"]) if payload.get("source_row") is not None else None),
            affected_fields=tuple(str(item) for item in payload.get("affected_fields", ())),
            policy=QualityOutcomePolicy(str(payload["policy"])),
            owner_role=QualityOwnerRole(str(payload["owner_role"])),
            owner_label=str(payload.get("owner_label", "")),
            evidence_display=str(payload.get("evidence_display", "masked")),
        )


@dataclass(frozen=True, slots=True)
class QualityRowResult:
    row_id: str
    dataset: str
    source_row: int
    record_label: str
    base_disposition: QualityDisposition
    effective_disposition: QualityDisposition
    issue_ids: tuple[str, ...]
    requires_review: bool = False

    def __post_init__(self) -> None:
        _require_hash(self.row_id, "quality row ID")
        if not self.dataset or self.source_row < 1:
            raise ValueError("Quality row coordinates are invalid")
        if self.issue_ids != tuple(sorted(set(self.issue_ids))):
            raise ValueError("Quality row issue IDs must be unique and ordered")

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "record_label": self.record_label,
            "base_disposition": self.base_disposition.value,
            "effective_disposition": self.effective_disposition.value,
            "issue_ids": list(self.issue_ids),
            "requires_review": self.requires_review,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QualityRowResult":
        return cls(
            row_id=str(payload["row_id"]),
            dataset=str(payload["dataset"]),
            source_row=int(payload["source_row"]),
            record_label=str(payload.get("record_label", "")),
            base_disposition=QualityDisposition(str(payload["base_disposition"])),
            effective_disposition=QualityDisposition(str(payload["effective_disposition"])),
            issue_ids=tuple(str(item) for item in payload.get("issue_ids", ())),
            requires_review=bool(payload.get("requires_review", False)),
        )


@dataclass(frozen=True, slots=True)
class SourceAccountingEntry:
    physical_dataset_id: str
    source_row: int
    state: SourceAccountingState
    canonical_row_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.physical_dataset_id or self.source_row < 1:
            raise ValueError("Source accounting coordinates are invalid")
        if self.canonical_row_ids != tuple(sorted(set(self.canonical_row_ids))):
            raise ValueError("Source accounting links must be unique and ordered")
        if self.state is SourceAccountingState.REPRESENTED and not self.canonical_row_ids:
            raise ValueError("Represented source rows require canonical links")
        if self.state is not SourceAccountingState.REPRESENTED and self.canonical_row_ids:
            raise ValueError("Unrepresented source states cannot contain canonical links")

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "physical_dataset_id": self.physical_dataset_id,
            "source_row": self.source_row,
            "state": self.state.value,
            "canonical_row_ids": list(self.canonical_row_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceAccountingEntry":
        return cls(
            physical_dataset_id=str(payload["physical_dataset_id"]),
            source_row=int(payload["source_row"]),
            state=SourceAccountingState(str(payload["state"])),
            canonical_row_ids=tuple(str(item) for item in payload.get("canonical_row_ids", ())),
        )


@dataclass(frozen=True, slots=True)
class QuarantineEntry:
    entry_id: str
    row_id: str
    dataset: str
    source_row: int
    physical_sources: tuple[str, ...]
    issue_id: str
    rule_id: str
    reason_code: str
    explanation: str
    affected_fields: tuple[str, ...]
    owner_role: QualityOwnerRole
    owner_label: str
    review_by: str | None
    correction_route: str

    def __post_init__(self) -> None:
        for value, label in ((self.entry_id, "quarantine entry ID"), (self.row_id, "quarantine row ID"), (self.issue_id, "quarantine issue ID"), (self.rule_id, "quarantine rule ID")):
            _require_hash(value, label)
        if not self.dataset or self.source_row < 1 or not self.reason_code:
            raise ValueError("Quarantine evidence is incomplete")
        if self.physical_sources != tuple(sorted(set(self.physical_sources))):
            raise ValueError("Quarantine physical sources must be unique and ordered")

    def to_portable_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "row_id": self.row_id,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "physical_sources": list(self.physical_sources),
            "issue_id": self.issue_id,
            "rule_id": self.rule_id,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "affected_fields": list(self.affected_fields),
            "owner_role": self.owner_role.value,
            "owner_label": self.owner_label,
            "review_by": self.review_by,
            "correction_route": self.correction_route,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QuarantineEntry":
        return cls(
            entry_id=str(payload["entry_id"]),
            row_id=str(payload["row_id"]),
            dataset=str(payload["dataset"]),
            source_row=int(payload["source_row"]),
            physical_sources=tuple(str(item) for item in payload.get("physical_sources", ())),
            issue_id=str(payload["issue_id"]),
            rule_id=str(payload["rule_id"]),
            reason_code=str(payload["reason_code"]),
            explanation=str(payload["explanation"]),
            affected_fields=tuple(str(item) for item in payload.get("affected_fields", ())),
            owner_role=QualityOwnerRole(str(payload["owner_role"])),
            owner_label=str(payload.get("owner_label", "")),
            review_by=(str(payload["review_by"]) if payload.get("review_by") else None),
            correction_route=str(payload["correction_route"]),
        )


@dataclass(frozen=True, slots=True)
class QualityRun:
    project_id: str
    staging_content_hash: str
    ruleset_hash: str
    mapping_hash: str
    schema_hash: str
    retention_context_hash: str
    row_results: tuple[QualityRowResult, ...]
    source_accounting: tuple[SourceAccountingEntry, ...]
    issues: tuple[QualityIssue, ...]
    quarantine: tuple[QuarantineEntry, ...]
    evaluator_version: int = QUALITY_EVALUATOR_VERSION
    contract_version: int = QUALITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != QUALITY_CONTRACT_VERSION or self.evaluator_version != QUALITY_EVALUATOR_VERSION:
            raise ValueError("Quality evidence version is unsupported")
        if not self.project_id:
            raise ValueError("Quality evidence identity is incomplete")
        for value, label in ((self.staging_content_hash, "quality staging hash"), (self.ruleset_hash, "quality ruleset hash"), (self.mapping_hash, "quality mapping hash"), (self.schema_hash, "quality schema hash"), (self.retention_context_hash, "quality retention hash")):
            _require_hash(value, label)
        if self.row_results != tuple(sorted(self.row_results, key=lambda item: (item.dataset, item.source_row, item.row_id))):
            raise ValueError("Quality rows must use deterministic ordering")
        if len({item.row_id for item in self.row_results}) != len(self.row_results):
            raise ValueError("Every canonical row requires exactly one quality result")
        if self.source_accounting != tuple(sorted(self.source_accounting, key=lambda item: (item.physical_dataset_id, item.source_row))):
            raise ValueError("Source accounting must use deterministic ordering")
        coordinates = {(item.physical_dataset_id, item.source_row) for item in self.source_accounting}
        if len(coordinates) != len(self.source_accounting):
            raise ValueError("Every physical source row requires one accounting entry")
        if self.issues != tuple(sorted(self.issues, key=lambda item: item.issue_id)) or len({item.issue_id for item in self.issues}) != len(self.issues):
            raise ValueError("Quality issues must be unique and deterministically ordered")
        if self.quarantine != tuple(sorted(self.quarantine, key=lambda item: item.entry_id)) or len({item.entry_id for item in self.quarantine}) != len(self.quarantine):
            raise ValueError("Quarantine entries must be unique and deterministically ordered")
        row_ids = {item.row_id for item in self.row_results}
        issue_ids = {item.issue_id for item in self.issues}
        linked_rows = {row_id for item in self.source_accounting for row_id in item.canonical_row_ids}
        if linked_rows != row_ids:
            raise ValueError("Source accounting does not reconcile every canonical row")
        for row in self.row_results:
            if set(row.issue_ids) - issue_ids:
                raise ValueError("A quality row points to missing issue evidence")
        for item in self.quarantine:
            if item.row_id not in row_ids or item.issue_id not in issue_ids:
                raise ValueError("Quarantine evidence has an unresolved pointer")

    @property
    def content_hash(self) -> str:
        return _hash(self.to_portable_dict(include_hash=False))

    @property
    def ready_count(self) -> int:
        return sum(item.effective_disposition in {QualityDisposition.CANDIDATE, QualityDisposition.REFERENCE} and not item.requires_review for item in self.row_results)

    @property
    def review_count(self) -> int:
        return sum(item.requires_review for item in self.row_results)

    @property
    def quarantined_count(self) -> int:
        return sum(item.effective_disposition is QualityDisposition.QUARANTINED for item in self.row_results)

    @property
    def excluded_count(self) -> int:
        return sum(item.effective_disposition is QualityDisposition.EXCLUDED for item in self.row_results)

    @property
    def blocked_count(self) -> int:
        return sum(item.effective_disposition is QualityDisposition.BLOCKED for item in self.row_results) + sum(item.row_id is None and item.policy is QualityOutcomePolicy.BLOCK for item in self.issues) + sum(item.state is SourceAccountingState.UNREPRESENTED for item in self.source_accounting)

    @property
    def can_compare(self) -> bool:
        return self.blocked_count == 0

    @property
    def ready_for_package(self) -> bool:
        return self.can_compare and self.review_count == 0

    @property
    def eligible_row_ids(self) -> frozenset[str]:
        return frozenset(item.row_id for item in self.row_results if item.effective_disposition in {QualityDisposition.CANDIDATE, QualityDisposition.REFERENCE})

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        payload = {
            "contract_version": self.contract_version,
            "evaluator_version": self.evaluator_version,
            "project_id": self.project_id,
            "staging_content_hash": self.staging_content_hash,
            "ruleset_hash": self.ruleset_hash,
            "mapping_hash": self.mapping_hash,
            "schema_hash": self.schema_hash,
            "retention_context_hash": self.retention_context_hash,
            "row_results": [item.to_portable_dict() for item in self.row_results],
            "source_accounting": [item.to_portable_dict() for item in self.source_accounting],
            "issues": [item.to_portable_dict() for item in self.issues],
            "quarantine": [item.to_portable_dict() for item in self.quarantine],
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        return canonical_json_bytes(self.to_portable_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QualityRun":
        run = cls(
            contract_version=int(payload.get("contract_version", 0)),
            evaluator_version=int(payload.get("evaluator_version", 0)),
            project_id=str(payload["project_id"]),
            staging_content_hash=str(payload["staging_content_hash"]),
            ruleset_hash=str(payload["ruleset_hash"]),
            mapping_hash=str(payload["mapping_hash"]),
            schema_hash=str(payload["schema_hash"]),
            retention_context_hash=str(payload["retention_context_hash"]),
            row_results=tuple(QualityRowResult.from_dict(item) for item in payload.get("row_results", ())),
            source_accounting=tuple(SourceAccountingEntry.from_dict(item) for item in payload.get("source_accounting", ())),
            issues=tuple(QualityIssue.from_dict(item) for item in payload.get("issues", ())),
            quarantine=tuple(QuarantineEntry.from_dict(item) for item in payload.get("quarantine", ())),
        )
        if payload.get("content_hash") != run.content_hash:
            raise ValueError("Quality content hash is invalid")
        return run

    @classmethod
    def from_json(cls, value: str) -> "QualityRun":
        return cls.from_dict(json.loads(value))


@dataclass(frozen=True, slots=True)
class QualityRunSummary:
    run_id: str
    project_id: str
    content_hash: str
    staging_run_id: str
    staging_content_hash: str
    ruleset_hash: str
    status: QualityRunStatus
    published_at: datetime
    published_by: str
    ready_count: int
    review_count: int
    quarantined_count: int
    excluded_count: int
    blocked_count: int

    @property
    def can_compare(self) -> bool:
        return self.blocked_count == 0

    @property
    def ready_for_package(self) -> bool:
        return self.can_compare and self.review_count == 0


@dataclass(frozen=True, slots=True)
class QualityReviewItem:
    row: QualityRowResult
    issues: tuple[QualityIssue, ...]
    correction_route: str = ""


@dataclass(frozen=True, slots=True)
class QualityReviewPage:
    items: tuple[QualityReviewItem, ...]
    matching_count: int
    page: int
    page_count: int


def default_quality_ruleset(
    *,
    project_id: str,
    mapping_hash: str,
    schema_hash: str,
    datasets: Iterable[str],
    version: int = 1,
    parent_version: int | None = None,
    manager_rules: Iterable[QualityRule] = (),
) -> QualityRuleSet:
    """Build the complete allowlisted automatic rules plus guided business rules."""

    rules: list[QualityRule] = []
    labels = {
        QualityRuleFamily.REQUIRED_VALUES: ("Required values", "Set aside records that are missing a value required by the confirmed field matches."),
        QualityRuleFamily.BOUNDED_VALUES: ("Valid values", "Set aside records whose prepared value has an invalid type, length, format or allowed choice."),
        QualityRuleFamily.GOVERNED_LOOKUPS: ("Known choices", "Set aside records when a governed source choice cannot be matched uniquely."),
        QualityRuleFamily.RELATIONSHIP_READINESS: ("Linked records", "Set aside records whose required linked record is missing, ambiguous or already set aside."),
        QualityRuleFamily.IDENTITY_COLLISION: ("Duplicate Odoo match", "Set aside every prepared record that would use the same Odoo business identity and scope."),
    }
    for dataset in sorted(set(datasets)):
        for family in QualityRuleFamily:
            if family.value not in MANDATORY_QUALITY_FAMILIES:
                continue
            name, explanation = labels[family]
            rules.append(
                QualityRule(
                    rule_id=_rule_id(project_id, dataset, family, name),
                    dataset=dataset,
                    family=family,
                    name=name,
                    explanation=explanation,
                    input_fields=(),
                    parameters={},
                    outcome=QualityOutcomePolicy.QUARANTINE,
                    owner_role=QualityOwnerRole.DATA_MANAGER,
                    source=QualityRuleSource.MAPPING_DERIVED,
                )
            )
    rules.extend(manager_rules)
    ruleset_id = "quality:" + sha256(project_id.encode("utf-8")).hexdigest()[:32]
    return QualityRuleSet(
        ruleset_id=ruleset_id,
        project_id=project_id,
        version=version,
        parent_version=parent_version,
        mapping_hash=mapping_hash,
        schema_hash=schema_hash,
        rules=tuple(sorted(rules, key=lambda item: item.rule_id)),
    )


def manager_quality_rule(
    *,
    project_id: str,
    dataset: str,
    family: QualityRuleFamily,
    name: str,
    input_fields: Iterable[str],
    parameters: Mapping[str, str] | None = None,
    outcome: QualityOutcomePolicy = QualityOutcomePolicy.QUARANTINE,
    owner_role: QualityOwnerRole = QualityOwnerRole.DATA_MANAGER,
) -> QualityRule:
    if family.value in MANDATORY_QUALITY_FAMILIES:
        raise ValueError("Business checks must use a guided cross-field rule")
    fields = tuple(input_fields)
    return QualityRule(
        rule_id=_rule_id(project_id, dataset, family, name, fields, parameters or {}, outcome.value, owner_role.value),
        dataset=dataset,
        family=family,
        name=name,
        explanation=_business_rule_explanation(family),
        input_fields=fields,
        parameters=dict(parameters or {}),
        outcome=outcome,
        owner_role=owner_role,
        source=QualityRuleSource.MANAGER_AUTHORED,
    )


def retention_context_hash(project: MigrationProject) -> str:
    return _hash(
        {
            "project_id": project.project_id,
            "data_manager": project.data_manager,
            "functional_owner": project.functional_owner,
            "retention_days": project.retention_days,
            "data_classification": project.data_classification.value,
        }
    )


def evaluate_quality(
    *,
    project: MigrationProject,
    staging: CanonicalStagingRun,
    prepared: PreparedBundle,
    physical_rows: Mapping[str, tuple[int, ...]],
    ruleset: QualityRuleSet,
) -> QualityRun:
    """Evaluate a complete quality overlay without database or Odoo access."""

    if staging.project_id != project.project_id or ruleset.project_id != project.project_id:
        raise QualityError("Quality evidence belongs to another project")
    if ruleset.mapping_hash != staging.mapping_hash or ruleset.schema_hash != staging.schema_hash:
        raise QualityError("Data checks no longer match the submitted field matches")
    rows_by_id = {row.row_id: row for row in staging.rows}
    prepared_by_coordinate: dict[tuple[str, int], list[PreparedRecord]] = {}
    for record in prepared.records:
        prepared_by_coordinate.setdefault((record.dataset, record.source_row), []).append(record)
    canonical_by_coordinate: dict[tuple[str, int], list[CanonicalRow]] = {}
    for row in staging.rows:
        canonical_by_coordinate.setdefault((row.dataset, row.source_row), []).append(row)
    rules_by_family = {(rule.dataset, rule.family): rule for rule in ruleset.rules if rule.source is not QualityRuleSource.MANAGER_AUTHORED}
    issue_map: dict[str, QualityIssue] = {}
    row_issue_ids: dict[str, set[str]] = {row.row_id: set() for row in staging.rows}

    missing = [
        (dataset.dataset, family)
        for dataset in staging.datasets
        for family in QualityRuleFamily
        if family.value in MANDATORY_QUALITY_FAMILIES and (dataset.dataset, family) not in rules_by_family
    ]
    for dataset, family in missing:
        issue = _setup_issue(project, dataset, family, "QUALITY_RULE_MISSING", "An automatic data check is missing. Restore the recommended checks before continuing.")
        issue_map[issue.issue_id] = issue
    available_fields: dict[str, set[str]] = {}
    for row in staging.rows:
        available_fields.setdefault(row.dataset, set()).update(
            row.proposed_values
        )
    invalid_manager_rules = {
        rule.rule_id
        for rule in ruleset.manager_rules
        if set(rule.input_fields) - available_fields.get(rule.dataset, set())
    }
    for rule in ruleset.manager_rules:
        if rule.rule_id not in invalid_manager_rules:
            continue
        issue = _setup_issue(
            project,
            rule.dataset,
            rule.family,
            "QUALITY_RULE_FIELD_MISSING",
            f"The optional check {rule.name!r} uses a field that is no longer prepared. Update or remove the check.",
            rule_id=rule.rule_id,
        )
        issue_map[issue.issue_id] = issue

    for row in staging.rows:
        for item in row.issues:
            family = _family_for_issue(item)
            rule = rules_by_family.get((row.dataset, family))
            if rule is None:
                continue
            policy = QualityOutcomePolicy.WARNING if item.severity == "warning" else rule.outcome
            issue = _quality_issue(project, rule, row, item.code, item.message, (item.field,) if item.field else (), policy=policy)
            issue_map[issue.issue_id] = issue
            row_issue_ids[row.row_id].add(issue.issue_id)

    for item in staging.issues:
        if item.dataset and item.source_row is not None:
            for row in canonical_by_coordinate.get((item.dataset, item.source_row), ()):
                family = _family_for_issue(item)
                rule = rules_by_family.get((row.dataset, family))
                if rule is None:
                    continue
                policy = QualityOutcomePolicy.WARNING if item.severity == "warning" else rule.outcome
                issue = _quality_issue(project, rule, row, item.code, item.message, (item.field,) if item.field else (), policy=policy)
                issue_map[issue.issue_id] = issue
                row_issue_ids[row.row_id].add(issue.issue_id)
        elif item.dataset:
            issue = _setup_issue(project, item.dataset, _family_for_issue(item), item.code, item.message)
            issue_map[issue.issue_id] = issue

    collision_groups: dict[bytes, list[CanonicalRow]] = {}
    for row in staging.rows:
        identity = (*row.target_identity, *row.target_scope)
        if not identity or any(value is None or value == "" for value in identity):
            continue
        key = canonical_json_bytes({"dataset": row.dataset, "model": row.target_model, "identity": portable_value(row.target_identity), "scope": portable_value(row.target_scope)})
        collision_groups.setdefault(key, []).append(row)
    for group in collision_groups.values():
        if len(group) < 2:
            continue
        for row in group:
            rule = rules_by_family.get((row.dataset, QualityRuleFamily.IDENTITY_COLLISION))
            if rule is None:
                continue
            issue = _quality_issue(project, rule, row, "POST_TRANSFORM_IDENTITY_COLLISION", f"{len(group)} prepared records would use the same Odoo match. All were set aside for review.", (), policy=rule.outcome)
            issue_map[issue.issue_id] = issue
            row_issue_ids[row.row_id].add(issue.issue_id)

    for rule in (
        item
        for item in ruleset.rules
        if item.source is QualityRuleSource.MANAGER_AUTHORED
        and item.rule_id not in invalid_manager_rules
    ):
        for row in (item for item in staging.rows if item.dataset == rule.dataset):
            failed, reason = _business_rule_failed(rule, row)
            if not failed:
                continue
            issue = _quality_issue(project, rule, row, "BUSINESS_CHECK_FAILED", reason, rule.input_fields, policy=rule.outcome)
            issue_map[issue.issue_id] = issue
            row_issue_ids[row.row_id].add(issue.issue_id)

    # Relationship checks are propagated until a stable set is reached.  The
    # indexes are built once, so no source-row or Odoo query occurs in the loop.
    source_index: dict[tuple[str, bytes], list[str]] = {}
    for row in staging.rows:
        source_index.setdefault((row.dataset, canonical_json_bytes(portable_value(row.source_identity))), []).append(row.row_id)
    changed = True
    while changed:
        changed = False
        dispositions = {row.row_id: _effective_disposition(row, (issue_map[item] for item in row_issue_ids[row.row_id])) for row in staging.rows}
        for coordinate, records in prepared_by_coordinate.items():
            canonical_rows = canonical_by_coordinate.get(coordinate, ())
            if len(records) != 1 or len(canonical_rows) != 1:
                continue
            row = canonical_rows[0]
            rule = rules_by_family.get((row.dataset, QualityRuleFamily.RELATIONSHIP_READINESS))
            if rule is None:
                continue
            for reference in _logical_references(records[0]):
                if not reference.dataset:
                    continue
                matches = source_index.get((reference.dataset, canonical_json_bytes(portable_value(reference.key))), ())
                unsafe = len(matches) != 1 or dispositions.get(matches[0]) in {QualityDisposition.BLOCKED, QualityDisposition.QUARANTINED, QualityDisposition.EXCLUDED}
                if not unsafe:
                    continue
                message = "The linked incoming record is missing, ambiguous or set aside. This dependent record was also set aside."
                issue = _quality_issue(project, rule, row, "INCOMING_RELATIONSHIP_NOT_READY", message, (), policy=rule.outcome)
                if issue.issue_id not in row_issue_ids[row.row_id]:
                    issue_map[issue.issue_id] = issue
                    row_issue_ids[row.row_id].add(issue.issue_id)
                    changed = True

    row_results = tuple(
        sorted(
            (
                QualityRowResult(
                    row_id=row.row_id,
                    dataset=row.dataset,
                    source_row=row.source_row,
                    record_label=_record_label(row),
                    base_disposition=QualityDisposition(row.disposition.value),
                    effective_disposition=_effective_disposition(row, (issue_map[item] for item in row_issue_ids[row.row_id])),
                    issue_ids=tuple(sorted(row_issue_ids[row.row_id])),
                    requires_review=any(issue_map[item].policy is QualityOutcomePolicy.WARNING for item in row_issue_ids[row.row_id]),
                )
                for row in staging.rows
            ),
            key=lambda item: (item.dataset, item.source_row, item.row_id),
        )
    )

    known_physical = {(dataset_id, row_number) for dataset_id, row_numbers in physical_rows.items() for row_number in row_numbers}
    links: dict[tuple[str, int], set[str]] = {coordinate: set() for coordinate in known_physical}
    for row in staging.rows:
        for source_row in row.lineage.physical_source_rows:
            coordinate = (row.lineage.physical_dataset_id, source_row)
            if coordinate not in links:
                raise QualityError("Canonical lineage points outside the frozen physical rows")
            links[coordinate].add(row.row_id)
    source_accounting = tuple(
        SourceAccountingEntry(
            physical_dataset_id=dataset_id,
            source_row=source_row,
            state=(SourceAccountingState.REPRESENTED if row_ids else SourceAccountingState.UNREPRESENTED),
            canonical_row_ids=tuple(sorted(row_ids)),
        )
        for (dataset_id, source_row), row_ids in sorted(links.items())
    )

    physical_by_row: dict[str, tuple[str, ...]] = {}
    for row in staging.rows:
        physical_by_row[row.row_id] = tuple(sorted(f"{row.lineage.physical_dataset_id}:{item}" for item in row.lineage.physical_source_rows))
    quarantine = []
    for result in row_results:
        if result.effective_disposition is not QualityDisposition.QUARANTINED:
            continue
        for issue_id in result.issue_ids:
            issue = issue_map[issue_id]
            if issue.policy is not QualityOutcomePolicy.QUARANTINE:
                continue
            row = rows_by_id[result.row_id]
            entry_id = _hash({"staging": staging.content_hash, "row_id": row.row_id, "issue_id": issue.issue_id})
            quarantine.append(
                QuarantineEntry(
                    entry_id=entry_id,
                    row_id=row.row_id,
                    dataset=row.dataset,
                    source_row=row.source_row,
                    physical_sources=physical_by_row[row.row_id],
                    issue_id=issue.issue_id,
                    rule_id=issue.rule_id,
                    reason_code=issue.reason_code,
                    explanation=issue.message,
                    affected_fields=issue.affected_fields,
                    owner_role=issue.owner_role,
                    owner_label=issue.owner_label,
                    review_by=None,
                    correction_route=_correction_route(issue.family),
                )
            )

    return QualityRun(
        project_id=project.project_id,
        staging_content_hash=staging.content_hash,
        ruleset_hash=ruleset.content_hash,
        mapping_hash=staging.mapping_hash,
        schema_hash=staging.schema_hash,
        retention_context_hash=retention_context_hash(project),
        row_results=row_results,
        source_accounting=source_accounting,
        issues=tuple(sorted(issue_map.values(), key=lambda item: item.issue_id)),
        quarantine=tuple(sorted(quarantine, key=lambda item: item.entry_id)),
    )


def eligible_prepared_bundle(
    staging: CanonicalStagingRun,
    prepared: PreparedBundle,
    quality: QualityRun,
) -> PreparedBundle:
    """Return the compatibility bundle allowed to enter read-only preflight."""

    allowed_coordinates = {
        (row.dataset, row.source_row)
        for row in staging.rows
        if row.row_id in quality.eligible_row_ids
    }
    return PreparedBundle(
        records=tuple(record for record in prepared.records if (record.dataset, record.source_row) in allowed_coordinates),
        issues=(),
        source_hashes=prepared.source_hashes,
    )


def _quality_issue(
    project: MigrationProject,
    rule: QualityRule,
    row: CanonicalRow,
    reason_code: str,
    message: str,
    fields: Iterable[str],
    *,
    policy: QualityOutcomePolicy,
) -> QualityIssue:
    affected_fields = tuple(sorted(set(item for item in fields if item)))
    issue_id = _hash({"rule_id": rule.rule_id, "row_id": row.row_id, "reason_code": reason_code, "message": message, "fields": affected_fields, "policy": policy.value})
    return QualityIssue(
        issue_id=issue_id,
        rule_id=rule.rule_id,
        family=rule.family,
        reason_code=reason_code,
        message=message,
        dataset=row.dataset,
        row_id=row.row_id,
        source_row=row.source_row,
        affected_fields=affected_fields,
        policy=policy,
        owner_role=rule.owner_role,
        owner_label=_owner_label(project, rule.owner_role),
        evidence_display=rule.evidence_display,
    )


def _setup_issue(
    project: MigrationProject,
    dataset: str,
    family: QualityRuleFamily,
    reason_code: str,
    message: str,
    *,
    rule_id: str | None = None,
) -> QualityIssue:
    rule_id = rule_id or _rule_id(
        project.project_id,
        dataset,
        family,
        "Missing automatic check",
    )
    return QualityIssue(
        issue_id=_hash({"rule_id": rule_id, "reason_code": reason_code, "dataset": dataset, "message": message}),
        rule_id=rule_id,
        family=family,
        reason_code=reason_code,
        message=message,
        dataset=dataset,
        row_id=None,
        source_row=None,
        affected_fields=(),
        policy=QualityOutcomePolicy.BLOCK,
        owner_role=QualityOwnerRole.DATA_MANAGER,
        owner_label=_owner_label(project, QualityOwnerRole.DATA_MANAGER),
    )


def _effective_disposition(row: CanonicalRow, issues: Iterable[QualityIssue]) -> QualityDisposition:
    items = tuple(issues)
    policies = {item.policy for item in items}
    if QualityOutcomePolicy.BLOCK in policies:
        return QualityDisposition.BLOCKED
    if QualityOutcomePolicy.QUARANTINE in policies or row.disposition is StagingDisposition.BLOCKED:
        return QualityDisposition.QUARANTINED
    if QualityOutcomePolicy.EXCLUDE in policies:
        return QualityDisposition.EXCLUDED
    return QualityDisposition(row.disposition.value)


def _family_for_issue(issue: CanonicalIssue) -> QualityRuleFamily:
    code = issue.code.upper()
    if "REQUIRED" in code or "MISSING" in code:
        return QualityRuleFamily.REQUIRED_VALUES
    if "RELATION" in code or "REFERENCE" in code or "DERIVED" in code:
        return QualityRuleFamily.RELATIONSHIP_READINESS
    if "LOOKUP" in code or "VALUE_MAPPING" in code or "SELECTION" in code:
        return QualityRuleFamily.GOVERNED_LOOKUPS
    return QualityRuleFamily.BOUNDED_VALUES


def _business_rule_failed(rule: QualityRule, row: CanonicalRow) -> tuple[bool, str]:
    values = [row.proposed_values.get(field) for field in rule.input_fields]
    present = [value is not None and value != "" for value in values]
    if rule.family is QualityRuleFamily.REQUIRED_IF:
        expected = rule.parameters.get("equals", "")
        failed = len(values) == 2 and str(values[1] if values[1] is not None else "") == expected and not present[0]
        return failed, "A value is required because the related condition is met."
    if rule.family is QualityRuleFamily.EXACTLY_ONE_OF:
        return sum(present) != 1, "Exactly one of the selected fields must contain a value."
    if rule.family is QualityRuleFamily.ORDERED_COMPARISON:
        if len(values) != 2 or not all(present):
            return False, ""
        try:
            return values[0] > values[1], "The first selected value must not be greater than the second."
        except TypeError:
            return True, "The selected values cannot be compared safely."
    if rule.family is QualityRuleFamily.EQUALITY:
        return len(values) == 2 and values[0] != values[1], "The selected fields must contain the same value."
    if rule.family is QualityRuleFamily.INEQUALITY:
        return len(values) == 2 and values[0] == values[1], "The selected fields must contain different values."
    raise QualityError("A manager-authored data check uses an unsupported rule")


def _logical_references(record: PreparedRecord) -> tuple[LogicalReference, ...]:
    found: list[LogicalReference] = []

    def collect(value: object) -> None:
        if isinstance(value, LogicalReference):
            found.append(value)
        elif isinstance(value, tuple):
            for item in value:
                collect(item)

    for value in (*record.target_identity, *record.target_scope, *record.references.values()):
        collect(value)
    return tuple(found)


def _owner_label(project: MigrationProject, role: QualityOwnerRole) -> str:
    if role is QualityOwnerRole.FUNCTIONAL_OWNER:
        return project.functional_owner or "Functional owner"
    return project.data_manager or "Data manager"


def _record_label(row: CanonicalRow) -> str:
    values = [value for value in (*row.target_identity, *row.source_identity) if value is not None and value != ""]
    if not values:
        return f"Row {row.source_row}"
    label = " / ".join(str(item) for item in values[:2])
    return label[:120]


def _correction_route(family: QualityRuleFamily) -> str:
    if family in {QualityRuleFamily.REQUIRED_IF, QualityRuleFamily.EXACTLY_ONE_OF, QualityRuleFamily.ORDERED_COMPARISON, QualityRuleFamily.EQUALITY, QualityRuleFamily.INEQUALITY}:
        return "Correct the source or change this guided data check, then check all rows again."
    if family is QualityRuleFamily.IDENTITY_COLLISION:
        return "Correct the source identity or field match, then check all rows again."
    if family is QualityRuleFamily.RELATIONSHIP_READINESS:
        return "Correct the linked source record or field match, then check all rows again."
    return "Correct the source or field match, then check all rows again."


def _business_rule_explanation(family: QualityRuleFamily) -> str:
    return {
        QualityRuleFamily.REQUIRED_IF: "Require the first selected field when the second equals a chosen value.",
        QualityRuleFamily.EXACTLY_ONE_OF: "Require exactly one of the selected fields to contain a value.",
        QualityRuleFamily.ORDERED_COMPARISON: "Require the first selected value to be less than or equal to the second.",
        QualityRuleFamily.EQUALITY: "Require the selected fields to contain the same value.",
        QualityRuleFamily.INEQUALITY: "Require the selected fields to contain different values.",
    }[family]


def _rule_id(project_id: str, dataset: str, family: QualityRuleFamily, name: str, *extra: object) -> str:
    return _hash({"project_id": project_id, "dataset": dataset, "family": family.value, "name": name, "extra": portable_value(extra)})


def _hash(payload: object) -> str:
    return "sha256:" + sha256(canonical_json_bytes(payload)).hexdigest()


def _require_hash(value: str, label: str) -> None:
    if len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError(f"{label} must be a canonical sha256 hash")
    try:
        int(value[7:], 16)
    except ValueError as error:
        raise ValueError(f"{label} must be a canonical sha256 hash") from error
