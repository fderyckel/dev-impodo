"""Deterministic, target-independent data-quality and quarantine evidence.

The quality overlay is deliberately separate from canonical staging.  It may
classify or set aside canonical records, but it never rewrites prepared values
or contacts Odoo.  All identifiers in the portable contract are business-side
hashes; numeric Odoo identifiers are forbidden by construction.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from hashlib import sha256
import json
from typing import AbstractSet, Any, Iterable, Mapping, Sequence

from impodo.domain.shared.models import LogicalReference, canonical_json_bytes, portable_value
from impodo.domain.workspace.workbench import WorkspaceState
from impodo.domain.resolution import EffectiveDataset
from impodo.domain.coverage import ReferenceBundle
from impodo.domain.preparation.staging_contracts import CanonicalIssue, CanonicalRow, CanonicalStagingRun, StagingDisposition


QUALITY_CONTRACT_VERSION = 3
QUALITY_EVALUATOR_VERSION = 3
QUALITY_RULESET_CONTRACT_VERSION = 3
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
    """Allowlisted automatic and manager-authored check algorithms."""

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
    LUHN_CHECKSUM = "LUHN_CHECKSUM"
    IBAN_MOD97 = "IBAN_MOD97"
    POSTAL_FORMAT = "POSTAL_FORMAT"
    DATE_WINDOW = "DATE_WINDOW"
    APPROVED_CODE_LIST = "APPROVED_CODE_LIST"
    METRIC_BOUNDARY = "METRIC_BOUNDARY"
    IQR_OUTLIER = "IQR_OUTLIER"


class QualityOutcomePolicy(StrEnum):
    """Effect of a failed rule on review and dataset eligibility."""

    WARNING = "WARNING"
    BLOCK = "BLOCK"
    QUARANTINE = "QUARANTINE"
    EXCLUDE = "EXCLUDE"


class QualityRuleSource(StrEnum):
    """Authority from which a quality rule was derived."""

    MAPPING_DERIVED = "MAPPING_DERIVED"
    SCHEMA_DERIVED = "SCHEMA_DERIVED"
    MANAGER_AUTHORED = "MANAGER_AUTHORED"
    SCOPE_APPROVED = "SCOPE_APPROVED"


class QualityOwnerRole(StrEnum):
    """Project role accountable for resolving a quality finding."""

    DATA_MANAGER = "DATA_MANAGER"
    FUNCTIONAL_OWNER = "FUNCTIONAL_OWNER"


class QualityDisposition(StrEnum):
    """Stage-F eligibility state assigned to one canonical row."""

    CANDIDATE = "CANDIDATE"
    REFERENCE = "REFERENCE"
    BLOCKED = "BLOCKED"
    QUARANTINED = "QUARANTINED"
    EXCLUDED = "EXCLUDED"


class SourceAccountingState(StrEnum):
    """How one physical source row reconciles to canonical output."""

    REPRESENTED = "REPRESENTED"
    QUARANTINED_BEFORE_TRANSFORM = "QUARANTINED_BEFORE_TRANSFORM"
    EXCLUDED_BY_RULE = "EXCLUDED_BY_RULE"
    UNREPRESENTED = "UNREPRESENTED"


class QualityRunStatus(StrEnum):
    """Persistence lifecycle of a published quality run."""

    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, slots=True)
class QualityRule:
    """One validated data check and its ownership/outcome policy.

    Rules are immutable inputs to :func:`evaluate_quality`. Automatic rules
    mirror mapping/schema constraints; guided manager rules add cross-field
    business checks without permitting arbitrary executable expressions.
    """

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
        advanced_families = {
            QualityRuleFamily.LUHN_CHECKSUM,
            QualityRuleFamily.IBAN_MOD97,
            QualityRuleFamily.POSTAL_FORMAT,
            QualityRuleFamily.DATE_WINDOW,
            QualityRuleFamily.APPROVED_CODE_LIST,
            QualityRuleFamily.METRIC_BOUNDARY,
            QualityRuleFamily.IQR_OUTLIER,
        }
        if self.source is QualityRuleSource.SCOPE_APPROVED:
            if self.family not in advanced_families:
                raise ValueError("Scope-approved checks must use an advanced family")
            _validate_advanced_rule(self)
        elif (
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
        """Serialize the rule into deterministic, JSON-safe evidence."""

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
        """Reconstruct and validate a rule from persisted evidence."""

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
    """Versioned rule contract bound to one mapping and schema hash."""

    ruleset_id: str
    workspace_id: str
    version: int
    parent_version: int | None
    mapping_hash: str
    schema_hash: str
    rules: tuple[QualityRule, ...]
    coverage_scope_hash: str | None = None
    reference_bundle_hash: str | None = None
    contract_version: int = QUALITY_RULESET_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != QUALITY_RULESET_CONTRACT_VERSION:
            raise ValueError("Quality-rule contract version is unsupported")
        if not self.ruleset_id or not self.workspace_id or self.version < 1:
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
            if item.source in {
                QualityRuleSource.MAPPING_DERIVED,
                QualityRuleSource.SCHEMA_DERIVED,
            }
        ]
        if len(set(automatic)) != len(automatic):
            raise ValueError("Automatic quality families must be unique per table")
        manager_counts: dict[str, int] = {}
        for item in self.rules:
            if item.source is QualityRuleSource.MANAGER_AUTHORED:
                manager_counts[item.dataset] = manager_counts.get(item.dataset, 0) + 1
        if any(item > MAX_MANAGER_RULES_PER_DATASET for item in manager_counts.values()):
            raise ValueError("Too many optional business checks were configured")
        advanced = any(
            item.source is QualityRuleSource.SCOPE_APPROVED for item in self.rules
        )
        if advanced:
            if self.coverage_scope_hash is None:
                raise ValueError("Advanced quality checks require an approved scope")
            _require_hash(self.coverage_scope_hash, "quality coverage-scope hash")
        if any(
            item.family is QualityRuleFamily.APPROVED_CODE_LIST
            for item in self.rules
        ):
            if self.reference_bundle_hash is None:
                raise ValueError("Approved-code checks require a reference bundle")
            _require_hash(
                self.reference_bundle_hash,
                "quality reference-bundle hash",
            )

    @property
    def content_hash(self) -> str:
        """Return the semantic hash used to bind evaluations to this ruleset."""

        payload = {
            "contract_version": self.contract_version,
            "workspace_id": self.workspace_id,
            "mapping_hash": self.mapping_hash,
            "schema_hash": self.schema_hash,
            "rules": [item.to_portable_dict() for item in self.rules],
        }
        payload["coverage_scope_hash"] = self.coverage_scope_hash
        payload["reference_bundle_hash"] = self.reference_bundle_hash
        return _hash(payload)

    @property
    def manager_rules(self) -> tuple[QualityRule, ...]:
        """Return only guided rules explicitly authored by a manager."""

        return tuple(item for item in self.rules if item.source is QualityRuleSource.MANAGER_AUTHORED)

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        """Serialize the complete ruleset, optionally including its hash."""

        payload = {
            "contract_version": self.contract_version,
            "ruleset_id": self.ruleset_id,
            "workspace_id": self.workspace_id,
            "version": self.version,
            "parent_version": self.parent_version,
            "mapping_hash": self.mapping_hash,
            "schema_hash": self.schema_hash,
            "rules": [item.to_portable_dict() for item in self.rules],
        }
        payload["coverage_scope_hash"] = self.coverage_scope_hash
        payload["reference_bundle_hash"] = self.reference_bundle_hash
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        """Return canonical JSON suitable for durable storage."""

        return canonical_json_bytes(self.to_portable_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QualityRuleSet":
        """Load a ruleset and verify its persisted content hash."""

        ruleset = cls(
            contract_version=int(payload["contract_version"]),
            ruleset_id=str(payload["ruleset_id"]),
            workspace_id=str(payload["workspace_id"]),
            version=int(payload["version"]),
            parent_version=(int(payload["parent_version"]) if payload.get("parent_version") is not None else None),
            mapping_hash=str(payload["mapping_hash"]),
            schema_hash=str(payload["schema_hash"]),
            rules=tuple(QualityRule.from_dict(item) for item in payload.get("rules", ())),
            coverage_scope_hash=(
                str(payload["coverage_scope_hash"])
                if payload.get("coverage_scope_hash") is not None
                else None
            ),
            reference_bundle_hash=(
                str(payload["reference_bundle_hash"])
                if payload.get("reference_bundle_hash") is not None
                else None
            ),
        )
        if payload.get("content_hash") != ruleset.content_hash:
            raise ValueError("Quality-rule content hash is invalid")
        return ruleset

    @classmethod
    def from_json(cls, value: str) -> "QualityRuleSet":
        """Load and validate a ruleset from canonical JSON."""

        return cls.from_dict(json.loads(value))


@dataclass(frozen=True, slots=True)
class QualityIssue:
    """One rule failure linked to a row or to run-level setup evidence."""

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
        """Serialize the finding and its correction ownership."""

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
        """Reconstruct a validated issue from persisted evidence."""

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
    """Stage-F disposition and finding links for one canonical row."""

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
        """Serialize one row-level eligibility decision."""

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
        """Reconstruct a row decision from persisted evidence."""

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
    """Reconciliation link from one physical row to canonical row IDs.

    This ledger proves that preparation did not silently lose input rows even
    when a row was quarantined before transformation or explicitly excluded.
    """

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
        """Serialize the physical-to-canonical accounting entry."""

        return {
            "physical_dataset_id": self.physical_dataset_id,
            "source_row": self.source_row,
            "state": self.state.value,
            "canonical_row_ids": list(self.canonical_row_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SourceAccountingEntry":
        """Reconstruct and validate one accounting entry."""

        return cls(
            physical_dataset_id=str(payload["physical_dataset_id"]),
            source_row=int(payload["source_row"]),
            state=SourceAccountingState(str(payload["state"])),
            canonical_row_ids=tuple(str(item) for item in payload.get("canonical_row_ids", ())),
        )


@dataclass(frozen=True, slots=True)
class QuarantineEntry:
    """Actionable correction record for a quarantined canonical row."""

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
        """Serialize quarantine reason, owner, deadline, and correction route."""

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
        """Reconstruct quarantine evidence from durable storage."""

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
    """Complete immutable Stage-F overlay on a canonical staging run.

    ``row_results`` decides eligibility, ``source_accounting`` proves complete
    coverage of physical input, ``issues`` explains every failure, and
    ``quarantine`` routes correctable records. The run is the input to Stage G.
    """

    workspace_id: str
    staging_content_hash: str
    ruleset_hash: str
    mapping_hash: str
    schema_hash: str
    retention_context_hash: str
    row_results: tuple[QualityRowResult, ...]
    source_accounting: tuple[SourceAccountingEntry, ...]
    issues: tuple[QualityIssue, ...]
    quarantine: tuple[QuarantineEntry, ...]
    effective_dataset_hash: str | None = None
    evaluator_version: int = QUALITY_EVALUATOR_VERSION
    contract_version: int = QUALITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            self.contract_version != QUALITY_CONTRACT_VERSION
            or self.evaluator_version != QUALITY_EVALUATOR_VERSION
        ):
            raise ValueError("Quality evidence version is unsupported")
        if self.effective_dataset_hash is None:
            raise ValueError("Current quality evidence requires an effective dataset")
        _require_hash(self.effective_dataset_hash, "quality effective-dataset hash")
        if not self.workspace_id:
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
        """Hash all semantic quality evidence for downstream binding."""

        return _hash(self.to_portable_dict(include_hash=False))

    @property
    def ready_count(self) -> int:
        """Count eligible rows that require no review."""

        return sum(item.effective_disposition in {QualityDisposition.CANDIDATE, QualityDisposition.REFERENCE} and not item.requires_review for item in self.row_results)

    @property
    def review_count(self) -> int:
        """Count eligible rows carrying warning-level review findings."""

        return sum(item.requires_review for item in self.row_results) + sum(
            item.row_id is None and item.policy is QualityOutcomePolicy.WARNING
            for item in self.issues
        )

    @property
    def quarantined_count(self) -> int:
        """Count rows removed from eligibility pending correction."""

        return sum(item.effective_disposition is QualityDisposition.QUARANTINED for item in self.row_results)

    @property
    def excluded_count(self) -> int:
        """Count rows removed by an explicit manager-authored exclusion rule."""

        return sum(item.effective_disposition is QualityDisposition.EXCLUDED for item in self.row_results)

    @property
    def blocked_count(self) -> int:
        """Count row, setup, and accounting failures that block comparison."""

        return sum(item.effective_disposition is QualityDisposition.BLOCKED for item in self.row_results) + sum(item.row_id is None and item.policy is QualityOutcomePolicy.BLOCK for item in self.issues) + sum(item.state is SourceAccountingState.UNREPRESENTED for item in self.source_accounting)

    @property
    def can_compare(self) -> bool:
        """Whether Stage G may compare prepared values without setup blockers."""

        return self.blocked_count == 0

    @property
    def ready_for_package(self) -> bool:
        """Whether no blocking or unresolved warning review remains."""

        return self.can_compare and self.review_count == 0

    @property
    def eligible_row_ids(self) -> frozenset[str]:
        """Return canonical row IDs retained for normalization and packaging."""

        return frozenset(item.row_id for item in self.row_results if item.effective_disposition in {QualityDisposition.CANDIDATE, QualityDisposition.REFERENCE})

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        """Serialize the complete quality run as portable evidence."""

        payload = {
            "contract_version": self.contract_version,
            "evaluator_version": self.evaluator_version,
            "workspace_id": self.workspace_id,
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
        payload["effective_dataset_hash"] = self.effective_dataset_hash
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        """Return the quality run as canonical JSON."""

        return canonical_json_bytes(self.to_portable_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QualityRun":
        """Load a run, enforcing reconciliation and its content hash."""

        run = cls(
            contract_version=int(payload["contract_version"]),
            evaluator_version=int(payload.get("evaluator_version", 0)),
            workspace_id=str(payload["workspace_id"]),
            staging_content_hash=str(payload["staging_content_hash"]),
            ruleset_hash=str(payload["ruleset_hash"]),
            mapping_hash=str(payload["mapping_hash"]),
            schema_hash=str(payload["schema_hash"]),
            retention_context_hash=str(payload["retention_context_hash"]),
            row_results=tuple(QualityRowResult.from_dict(item) for item in payload.get("row_results", ())),
            source_accounting=tuple(SourceAccountingEntry.from_dict(item) for item in payload.get("source_accounting", ())),
            issues=tuple(QualityIssue.from_dict(item) for item in payload.get("issues", ())),
            quarantine=tuple(QuarantineEntry.from_dict(item) for item in payload.get("quarantine", ())),
            effective_dataset_hash=(
                str(payload["effective_dataset_hash"])
                if payload.get("effective_dataset_hash") is not None
                else None
            ),
        )
        if payload.get("content_hash") != run.content_hash:
            raise ValueError("Quality content hash is invalid")
        return run

    @classmethod
    def from_json(cls, value: str) -> "QualityRun":
        """Load and validate a quality run from canonical JSON."""

        return cls.from_dict(json.loads(value))


@dataclass(frozen=True, slots=True)
class StoredQualityRun:
    """Validated Stage-F header backed by bounded evidence sequences.

    This is the quality equivalent of a stored canonical staging run.  It is
    intentionally not a second portable contract: repositories publish the
    same row, accounting, issue, and quarantine objects in the same canonical
    order while the application avoids retaining every object at once.
    """

    workspace_id: str
    staging_content_hash: str
    ruleset_hash: str
    mapping_hash: str
    schema_hash: str
    retention_context_hash: str
    row_results: Sequence[QualityRowResult]
    source_accounting: Sequence[SourceAccountingEntry]
    issues: Sequence[QualityIssue]
    quarantine: Sequence[QuarantineEntry]
    effective_dataset_hash: str
    eligible_row_ids: AbstractSet[str]
    summary_counts: Mapping[str, int]
    published_content_hash: str | None = None
    evaluator_version: int = QUALITY_EVALUATOR_VERSION
    contract_version: int = QUALITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            self.contract_version != QUALITY_CONTRACT_VERSION
            or self.evaluator_version != QUALITY_EVALUATOR_VERSION
        ):
            raise ValueError("Quality evidence version is unsupported")
        if not self.workspace_id:
            raise ValueError("Quality evidence identity is incomplete")
        for value, label in (
            (self.staging_content_hash, "quality staging hash"),
            (self.ruleset_hash, "quality ruleset hash"),
            (self.mapping_hash, "quality mapping hash"),
            (self.schema_hash, "quality schema hash"),
            (self.retention_context_hash, "quality retention hash"),
            (self.effective_dataset_hash, "quality effective-dataset hash"),
        ):
            _require_hash(value, label)
        if self.published_content_hash is not None:
            _require_hash(self.published_content_hash, "quality content hash")
        expected_counts = {
            "ready_count",
            "review_count",
            "quarantined_count",
            "excluded_count",
            "blocked_count",
        }
        if set(self.summary_counts) != expected_counts or any(
            not isinstance(value, int) or value < 0
            for value in self.summary_counts.values()
        ):
            raise ValueError("Quality summary counts are invalid")

    @property
    def content_hash(self) -> str:
        """Return the hash calculated while the bounded evidence was stored."""

        if self.published_content_hash is None:
            raise ValueError("Stored quality evidence has not been published")
        return self.published_content_hash

    def with_content_hash(self, content_hash: str) -> "StoredQualityRun":
        """Bind the exact repository-calculated hash after publication."""

        return replace(self, published_content_hash=content_hash)

    @property
    def ready_count(self) -> int:
        return self.summary_counts["ready_count"]

    @property
    def review_count(self) -> int:
        return self.summary_counts["review_count"]

    @property
    def quarantined_count(self) -> int:
        return self.summary_counts["quarantined_count"]

    @property
    def excluded_count(self) -> int:
        return self.summary_counts["excluded_count"]

    @property
    def blocked_count(self) -> int:
        return self.summary_counts["blocked_count"]

    @property
    def can_compare(self) -> bool:
        return self.blocked_count == 0

    @property
    def ready_for_package(self) -> bool:
        return self.can_compare and self.review_count == 0


@dataclass(frozen=True, slots=True)
class QualityRunSummary:
    """Small lifecycle/count projection for the currently published run."""

    run_id: str
    workspace_id: str
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
    effective_dataset_run_id: str | None = None
    effective_dataset_hash: str | None = None

    @property
    def can_compare(self) -> bool:
        """Mirror whether the persisted run has no comparison blockers."""

        return self.blocked_count == 0

    @property
    def ready_for_package(self) -> bool:
        """Mirror whether the run has neither blockers nor pending reviews."""

        return self.can_compare and self.review_count == 0


@dataclass(frozen=True, slots=True)
class QualityReviewItem:
    """One reviewable row with its resolved issue records and edit route."""

    row: QualityRowResult
    issues: tuple[QualityIssue, ...]
    correction_route: str = ""


@dataclass(frozen=True, slots=True)
class QualityReviewPage:
    """Bounded page of quality review items for browser navigation."""

    items: tuple[QualityReviewItem, ...]
    matching_count: int
    page: int
    page_count: int


def default_quality_ruleset(
    *,
    workspace_id: str,
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
                    rule_id=_rule_id(workspace_id, dataset, family, name),
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
    ruleset_id = "quality:" + sha256(workspace_id.encode("utf-8")).hexdigest()[:32]
    return QualityRuleSet(
        ruleset_id=ruleset_id,
        workspace_id=workspace_id,
        version=version,
        parent_version=parent_version,
        mapping_hash=mapping_hash,
        schema_hash=schema_hash,
        rules=tuple(sorted(rules, key=lambda item: item.rule_id)),
    )


def manager_quality_rule(
    *,
    workspace_id: str,
    dataset: str,
    family: QualityRuleFamily,
    name: str,
    input_fields: Iterable[str],
    parameters: Mapping[str, str] | None = None,
    outcome: QualityOutcomePolicy = QualityOutcomePolicy.QUARANTINE,
    owner_role: QualityOwnerRole = QualityOwnerRole.DATA_MANAGER,
) -> QualityRule:
    """Build one allowlisted guided business check with a stable identity."""

    if family.value in MANDATORY_QUALITY_FAMILIES:
        raise ValueError("Business checks must use a guided cross-field rule")
    fields = tuple(input_fields)
    return QualityRule(
        rule_id=_rule_id(workspace_id, dataset, family, name, fields, parameters or {}, outcome.value, owner_role.value),
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


def retention_context_hash(workspace_state: WorkspaceState) -> str:
    """Hash the retained classification and retention policy inputs."""

    return _hash(
        {
            "workspace_id": workspace_state.workspace_id,
            "retention_days": workspace_state.retention_days,
            "data_classification": workspace_state.data_classification.value,
        }
    )


def evaluate_quality(
    *,
    workspace_state: WorkspaceState,
    staging: CanonicalStagingRun,
    physical_rows: Mapping[str, tuple[int, ...]],
    ruleset: QualityRuleSet,
    published_staging_content_hash: str | None = None,
    effective: EffectiveDataset | None = None,
    reference_bundle: ReferenceBundle | None = None,
) -> QualityRun:
    """Evaluate a complete quality overlay without database or Odoo access."""

    if staging.workspace_id != workspace_state.workspace_id or ruleset.workspace_id != workspace_state.workspace_id:
        raise QualityError("Quality evidence belongs to another workspace")
    if ruleset.mapping_hash != staging.mapping_hash or ruleset.schema_hash != staging.schema_hash:
        raise QualityError("Data checks no longer match the submitted field matches")
    staging_content_hash = (
        published_staging_content_hash or staging.content_hash
    )
    if effective is not None and (
        effective.workspace_id != workspace_state.workspace_id
        or effective.staging_content_hash != staging_content_hash
    ):
        raise QualityError("Resolved data no longer matches current prepared data")
    effective_dataset_hash = (
        effective.content_hash if effective is not None else staging_content_hash
    )
    rows = tuple(
        item.canonical_row for item in effective.rows
    ) if effective is not None else staging.rows
    ruleset_hash = ruleset.content_hash
    if ruleset.reference_bundle_hash is not None and (
        reference_bundle is None
        or reference_bundle.workspace_id != workspace_state.workspace_id
        or reference_bundle.content_hash != ruleset.reference_bundle_hash
    ):
        raise QualityError("Approved reference data is missing or has changed")
    rows_by_id = {row.row_id: row for row in rows}
    canonical_by_coordinate: dict[
        tuple[str, int],
        CanonicalRow | tuple[CanonicalRow, ...],
    ] = {}
    for row in rows:
        coordinate = (row.dataset, row.source_row)
        existing = canonical_by_coordinate.get(coordinate)
        if existing is None:
            canonical_by_coordinate[coordinate] = row
        elif isinstance(existing, tuple):
            canonical_by_coordinate[coordinate] = (*existing, row)
        else:
            canonical_by_coordinate[coordinate] = (existing, row)
    rules_by_family = {(rule.dataset, rule.family): rule for rule in ruleset.rules if rule.source is not QualityRuleSource.MANAGER_AUTHORED}
    issue_map: dict[str, QualityIssue] = {}
    row_issue_ids: dict[str, set[str]] = {}

    missing = [
        (dataset.dataset, family)
        for dataset in staging.datasets
        for family in QualityRuleFamily
        if family.value in MANDATORY_QUALITY_FAMILIES and (dataset.dataset, family) not in rules_by_family
    ]
    for dataset, family in missing:
        issue = _setup_issue(workspace_state, dataset, family, "QUALITY_RULE_MISSING", "An automatic data check is missing. Restore the recommended checks before continuing.")
        issue_map[issue.issue_id] = issue
    available_fields: dict[str, set[str]] = {}
    rows_by_dataset: dict[str, list[CanonicalRow]] = {}
    for row in rows:
        available_fields.setdefault(row.dataset, set()).update(
            row.proposed_values
        )
        rows_by_dataset.setdefault(row.dataset, []).append(row)
    invalid_manager_rules = {
        rule.rule_id
        for rule in ruleset.manager_rules
        if set(rule.input_fields) - available_fields.get(rule.dataset, set())
    }
    invalid_advanced_rules = {
        rule.rule_id
        for rule in ruleset.rules
        if rule.source is QualityRuleSource.SCOPE_APPROVED
        and set(rule.input_fields) - available_fields.get(rule.dataset, set())
    }
    for rule in ruleset.manager_rules:
        if rule.rule_id not in invalid_manager_rules:
            continue
        issue = _setup_issue(
            workspace_state,
            rule.dataset,
            rule.family,
            "QUALITY_RULE_FIELD_MISSING",
            f"The optional check {rule.name!r} uses a field that is no longer prepared. Update or remove the check.",
            rule_id=rule.rule_id,
        )
        issue_map[issue.issue_id] = issue

    for rule in ruleset.rules:
        if rule.rule_id not in invalid_advanced_rules:
            continue
        issue = _setup_issue(
            workspace_state,
            rule.dataset,
            rule.family,
            "QUALITY_RULE_FIELD_MISSING",
            f"The approved check {rule.name!r} uses a field that is no longer prepared.",
            rule_id=rule.rule_id,
        )
        issue_map[issue.issue_id] = issue

    for row in rows:
        for item in row.issues:
            family = _family_for_issue(item)
            rule = rules_by_family.get((row.dataset, family))
            if rule is None:
                continue
            policy = QualityOutcomePolicy.WARNING if item.severity == "warning" else rule.outcome
            issue = _quality_issue(workspace_state, rule, row, item.code, item.message, (item.field,) if item.field else (), policy=policy)
            issue_map[issue.issue_id] = issue
            row_issue_ids.setdefault(row.row_id, set()).add(issue.issue_id)

    for item in staging.issues:
        if item.dataset and item.source_row is not None:
            matched = canonical_by_coordinate.get(
                (item.dataset, item.source_row)
            )
            matched_rows = (
                ()
                if matched is None
                else matched
                if isinstance(matched, tuple)
                else (matched,)
            )
            for row in matched_rows:
                family = _family_for_issue(item)
                rule = rules_by_family.get((row.dataset, family))
                if rule is None:
                    continue
                policy = QualityOutcomePolicy.WARNING if item.severity == "warning" else rule.outcome
                issue = _quality_issue(workspace_state, rule, row, item.code, item.message, (item.field,) if item.field else (), policy=policy)
                issue_map[issue.issue_id] = issue
                row_issue_ids.setdefault(row.row_id, set()).add(issue.issue_id)
        elif item.dataset:
            issue = _setup_issue(workspace_state, item.dataset, _family_for_issue(item), item.code, item.message)
            issue_map[issue.issue_id] = issue

    collision_groups: dict[bytes, CanonicalRow | list[CanonicalRow]] = {}
    for row in rows:
        key = quality_identity_key(row)
        if key is None:
            continue
        existing = collision_groups.get(key)
        if existing is None:
            collision_groups[key] = row
        elif isinstance(existing, list):
            existing.append(row)
        else:
            collision_groups[key] = [existing, row]
    for group in collision_groups.values():
        if not isinstance(group, list):
            continue
        for row in group:
            rule = rules_by_family.get((row.dataset, QualityRuleFamily.IDENTITY_COLLISION))
            if rule is None:
                continue
            issue = _quality_issue(workspace_state, rule, row, "POST_TRANSFORM_IDENTITY_COLLISION", f"{len(group)} prepared records would use the same Odoo match. All were set aside for review.", (), policy=rule.outcome)
            issue_map[issue.issue_id] = issue
            row_issue_ids.setdefault(row.row_id, set()).add(issue.issue_id)

    for rule in (
        item
        for item in ruleset.rules
        if item.source is QualityRuleSource.MANAGER_AUTHORED
        and item.rule_id not in invalid_manager_rules
    ):
        for row in rows_by_dataset.get(rule.dataset, ()):
            failed, reason = _business_rule_failed(rule, row)
            if not failed:
                continue
            issue = _quality_issue(workspace_state, rule, row, "BUSINESS_CHECK_FAILED", reason, rule.input_fields, policy=rule.outcome)
            issue_map[issue.issue_id] = issue
            row_issue_ids.setdefault(row.row_id, set()).add(issue.issue_id)

    for rule in (
        item
        for item in ruleset.rules
        if item.source is QualityRuleSource.SCOPE_APPROVED
        and item.rule_id not in invalid_advanced_rules
        and item.family not in {
            QualityRuleFamily.METRIC_BOUNDARY,
            QualityRuleFamily.IQR_OUTLIER,
        }
    ):
        for row in rows_by_dataset.get(rule.dataset, ()):
            failed, reason = _advanced_row_rule_failed(
                rule,
                row,
                reference_bundle,
            )
            if not failed:
                continue
            issue = _quality_issue(
                workspace_state,
                rule,
                row,
                "ADVANCED_CHECK_FAILED",
                reason,
                rule.input_fields,
                policy=rule.outcome,
            )
            issue_map[issue.issue_id] = issue
            row_issue_ids.setdefault(row.row_id, set()).add(issue.issue_id)

    for rule in (
        item
        for item in ruleset.rules
        if item.source is QualityRuleSource.SCOPE_APPROVED
        and item.rule_id not in invalid_advanced_rules
        and item.family is QualityRuleFamily.METRIC_BOUNDARY
    ):
        metric = _metric_value(rule, rows_by_dataset.get(rule.dataset, ()))
        failed = (
            "minimum" in rule.parameters
            and metric < Decimal(rule.parameters["minimum"])
        ) or (
            "maximum" in rule.parameters
            and metric > Decimal(rule.parameters["maximum"])
        )
        if failed:
            issue = _run_quality_issue(
                workspace_state,
                rule,
                "METRIC_BOUNDARY_FAILED",
                f"The governed {rule.parameters['metric']} metric was {format(metric, 'f')}, outside its approved boundary.",
            )
            issue_map[issue.issue_id] = issue

    for rule in (
        item
        for item in ruleset.rules
        if item.source is QualityRuleSource.SCOPE_APPROVED
        and item.rule_id not in invalid_advanced_rules
        and item.family is QualityRuleFamily.IQR_OUTLIER
    ):
        numeric_rows = []
        field = rule.input_fields[0]
        for row in rows_by_dataset.get(rule.dataset, ()):
            value = _finite_decimal(row.proposed_values.get(field))
            if value is not None:
                numeric_rows.append((row, value))
        if len(numeric_rows) < 4:
            continue
        ordered_values = sorted(item[1] for item in numeric_rows)
        q1 = _quartile(ordered_values, Decimal("0.25"))
        q3 = _quartile(ordered_values, Decimal("0.75"))
        spread = q3 - q1
        multiplier = Decimal(rule.parameters["multiplier"])
        lower = q1 - multiplier * spread
        upper = q3 + multiplier * spread
        for row, value in numeric_rows:
            if lower <= value <= upper:
                continue
            issue = _quality_issue(
                workspace_state,
                rule,
                row,
                "IQR_OUTLIER",
                "The value is outside the approved interquartile-range boundary.",
                rule.input_fields,
                policy=rule.outcome,
            )
            issue_map[issue.issue_id] = issue
            row_issue_ids.setdefault(row.row_id, set()).add(issue.issue_id)

    # Relationship checks use a dependency graph. Missing or ambiguous parents
    # are handled while the graph is built; a queue then visits each unsafe
    # parent and each parent-to-dependent edge at most once.
    source_index: dict[tuple[str, bytes], str | tuple[str, ...]] = {}
    for row in rows:
        key = (
            row.dataset,
            canonical_json_bytes(portable_value(row.source_identity)),
        )
        existing = source_index.get(key)
        if existing is None:
            source_index[key] = row.row_id
        elif isinstance(existing, tuple):
            source_index[key] = (*existing, row.row_id)
        else:
            source_index[key] = (existing, row.row_id)
    unsafe_dispositions = {
        QualityDisposition.BLOCKED,
        QualityDisposition.QUARANTINED,
        QualityDisposition.EXCLUDED,
    }
    dispositions = {
        row.row_id: _effective_disposition(
            row,
            (issue_map[item] for item in row_issue_ids.get(row.row_id, ())),
        )
        for row in rows
    }
    dependents_by_parent: dict[str, str | list[str]] = {}
    identity_parents_by_dependent: dict[str, str | list[str]] = {}
    unresolved_dependents: set[str] = set()
    relationship_rule_by_row: dict[str, QualityRule] = {}
    for row in rows:
        coordinate = (row.dataset, row.source_row)
        if isinstance(canonical_by_coordinate.get(coordinate), tuple):
            continue
        rule = rules_by_family.get(
            (row.dataset, QualityRuleFamily.RELATIONSHIP_READINESS)
        )
        if rule is None:
            continue
        relationship_rule_by_row[row.row_id] = rule
        seen_parent_ids: set[str] = set()
        for reference in _logical_references(row):
            if not reference.dataset:
                continue
            matches = source_index.get(
                (
                    reference.dataset,
                    canonical_json_bytes(portable_value(reference.key)),
                ),
                (),
            )
            if not isinstance(matches, str):
                unresolved_dependents.add(row.row_id)
                continue
            if matches in seen_parent_ids:
                continue
            seen_parent_ids.add(matches)
            existing = dependents_by_parent.get(matches)
            if existing is None:
                dependents_by_parent[matches] = row.row_id
            elif isinstance(existing, list):
                existing.append(row.row_id)
            else:
                dependents_by_parent[matches] = [existing, row.row_id]

        for reference in _identity_logical_references(row):
            if reference.origin != "incoming" or not reference.dataset:
                continue
            matches = source_index.get(
                (
                    reference.dataset,
                    canonical_json_bytes(portable_value(reference.key)),
                ),
                (),
            )
            if not isinstance(matches, str):
                continue
            existing = identity_parents_by_dependent.get(row.row_id)
            if existing is None:
                identity_parents_by_dependent[row.row_id] = matches
            elif isinstance(existing, list):
                if matches not in existing:
                    existing.append(matches)
            elif matches != existing:
                identity_parents_by_dependent[row.row_id] = [existing, matches]

    relationship_message = (
        "The linked incoming record is missing, ambiguous or set aside. "
        "This dependent record was also set aside."
    )
    identity_group_message = (
        "A dependent record that uses this incoming identity is missing, "
        "ambiguous or set aside. This record and its dependent group were "
        "also set aside."
    )

    def attach_relationship_issue(
        row_id: str,
        reason_code: str = "INCOMING_RELATIONSHIP_NOT_READY",
        message: str = relationship_message,
    ) -> bool:
        """Attach one deterministic issue and report a safe-to-unsafe change."""

        rule = relationship_rule_by_row.get(row_id)
        if rule is None:
            return False
        row = rows_by_id[row_id]
        issue = _quality_issue(
            workspace_state,
            rule,
            row,
            reason_code,
            message,
            (),
            policy=rule.outcome,
        )
        issue_ids = row_issue_ids.setdefault(row_id, set())
        if issue.issue_id in issue_ids:
            return False
        was_unsafe = dispositions[row_id] in unsafe_dispositions
        issue_map[issue.issue_id] = issue
        issue_ids.add(issue.issue_id)
        dispositions[row_id] = _effective_disposition(
            row,
            (issue_map[item] for item in issue_ids),
        )
        return not was_unsafe and dispositions[row_id] in unsafe_dispositions

    for row in rows:
        if row.row_id in unresolved_dependents:
            attach_relationship_issue(row.row_id)

    queue = deque(
        row.row_id
        for row in rows
        if dispositions[row.row_id] in unsafe_dispositions
    )
    while queue:
        parent_id = queue.popleft()
        dependents = dependents_by_parent.get(parent_id)
        dependent_ids = (
            ()
            if dependents is None
            else dependents
            if isinstance(dependents, list)
            else (dependents,)
        )
        for dependent_id in dependent_ids:
            became_unsafe = attach_relationship_issue(dependent_id)
            if became_unsafe:
                queue.append(dependent_id)
        identity_parents = identity_parents_by_dependent.get(parent_id)
        identity_parent_ids = (
            ()
            if identity_parents is None
            else identity_parents
            if isinstance(identity_parents, list)
            else (identity_parents,)
        )
        for identity_parent_id in identity_parent_ids:
            became_unsafe = attach_relationship_issue(
                identity_parent_id,
                "INCOMING_IDENTITY_GROUP_NOT_READY",
                identity_group_message,
            )
            if became_unsafe:
                queue.append(identity_parent_id)

    row_results = tuple(
        sorted(
            (
                QualityRowResult(
                    row_id=row.row_id,
                    dataset=row.dataset,
                    source_row=row.source_row,
                    record_label=_record_label(row),
                    base_disposition=QualityDisposition(row.disposition.value),
                    effective_disposition=_effective_disposition(row, (issue_map[item] for item in row_issue_ids.get(row.row_id, ()))),
                    issue_ids=tuple(sorted(row_issue_ids.get(row.row_id, ()))),
                    requires_review=any(issue_map[item].policy is QualityOutcomePolicy.WARNING for item in row_issue_ids.get(row.row_id, ())),
                )
                for row in rows
            ),
            key=lambda item: (item.dataset, item.source_row, item.row_id),
        )
    )

    known_physical = {(dataset_id, row_number) for dataset_id, row_numbers in physical_rows.items() for row_number in row_numbers}
    links: dict[tuple[str, int], set[str]] = {coordinate: set() for coordinate in known_physical}
    for row in rows:
        for physical_dataset_id, source_rows in row.lineage.physical_sources.items():
            for source_row in source_rows:
                coordinate = (physical_dataset_id, source_row)
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
    for row in rows:
        physical_by_row[row.row_id] = tuple(
            sorted(
                f"{dataset_id}:{source_row}"
                for dataset_id, source_rows in row.lineage.physical_sources.items()
                for source_row in source_rows
            )
        )
    quarantine = []
    for result in row_results:
        if result.effective_disposition is not QualityDisposition.QUARANTINED:
            continue
        for issue_id in result.issue_ids:
            issue = issue_map[issue_id]
            if issue.policy is not QualityOutcomePolicy.QUARANTINE:
                continue
            row = rows_by_id[result.row_id]
            entry_id = _hash({"effective_dataset": effective_dataset_hash, "row_id": row.row_id, "issue_id": issue.issue_id})
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
        workspace_id=workspace_state.workspace_id,
        staging_content_hash=staging_content_hash,
        ruleset_hash=ruleset_hash,
        mapping_hash=staging.mapping_hash,
        schema_hash=staging.schema_hash,
        retention_context_hash=retention_context_hash(workspace_state),
        row_results=row_results,
        source_accounting=source_accounting,
        issues=tuple(sorted(issue_map.values(), key=lambda item: item.issue_id)),
        quarantine=tuple(sorted(quarantine, key=lambda item: item.entry_id)),
        effective_dataset_hash=effective_dataset_hash,
    )


def _quality_issue(
    workspace_state: WorkspaceState,
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
        owner_label=_owner_label(workspace_state, rule.owner_role),
        evidence_display=rule.evidence_display,
    )


def _setup_issue(
    workspace_state: WorkspaceState,
    dataset: str,
    family: QualityRuleFamily,
    reason_code: str,
    message: str,
    *,
    rule_id: str | None = None,
) -> QualityIssue:
    rule_id = rule_id or _rule_id(
        workspace_state.workspace_id,
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
        owner_label=_owner_label(workspace_state, QualityOwnerRole.DATA_MANAGER),
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


def clean_quality_row_result(row: CanonicalRow) -> QualityRowResult:
    """Build the exact Stage-F result for a row with no quality findings."""

    disposition = QualityDisposition(row.disposition.value)
    return QualityRowResult(
        row_id=row.row_id,
        dataset=row.dataset,
        source_row=row.source_row,
        record_label=_record_label(row),
        base_disposition=disposition,
        effective_disposition=disposition,
        issue_ids=(),
        requires_review=False,
    )


def quality_identity_key(row: CanonicalRow) -> bytes | None:
    """Return the collision key used by the complete quality evaluator."""

    identity = (*row.target_identity, *row.target_scope)
    if not identity or any(value is None or value == "" for value in identity):
        return None
    return canonical_json_bytes(
        {
            "dataset": row.dataset,
            "model": row.target_model,
            "identity": portable_value(row.target_identity),
            "scope": portable_value(row.target_scope),
        }
    )


def has_logical_references(row: CanonicalRow) -> bool:
    """Whether relationship propagation requires the global graph evaluator."""

    return bool(_logical_references(row))


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


def _validate_advanced_rule(rule: QualityRule) -> None:
    """Reject unsupported parameters before any data is evaluated."""

    family = rule.family
    parameters = set(rule.parameters)
    if family in {QualityRuleFamily.LUHN_CHECKSUM, QualityRuleFamily.IBAN_MOD97}:
        if len(rule.input_fields) != 1 or parameters:
            raise ValueError("Checksum checks require one field and no parameters")
    elif family is QualityRuleFamily.POSTAL_FORMAT:
        if len(rule.input_fields) != 1 or parameters != {"jurisdiction"}:
            raise ValueError("Postal checks require one field and one jurisdiction")
        if rule.parameters["jurisdiction"].upper() not in {
            "BE", "DE", "FR", "GB", "LU", "NL",
        }:
            raise ValueError("Postal-check jurisdiction is unsupported")
    elif family is QualityRuleFamily.DATE_WINDOW:
        if len(rule.input_fields) != 1 or not parameters or not parameters <= {"minimum", "maximum"}:
            raise ValueError("Date-window parameters are invalid")
        try:
            for value in rule.parameters.values():
                date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("Date-window boundaries must be ISO dates") from error
    elif family is QualityRuleFamily.APPROVED_CODE_LIST:
        if not 1 <= len(rule.input_fields) <= 3 or parameters != {
            "reference_id", "reference_content_hash",
        }:
            raise ValueError("Approved-code parameters are invalid")
        _require_hash(
            rule.parameters["reference_content_hash"],
            "approved-code content hash",
        )
    elif family is QualityRuleFamily.METRIC_BOUNDARY:
        metric = rule.parameters.get("metric", "")
        if metric not in {
            "count", "distinct_count", "null_rate", "duplicate_rate",
            "minimum", "maximum", "sum",
        }:
            raise ValueError("Quality metric is unsupported")
        expected_fields = 0 if metric == "count" else 1
        if len(rule.input_fields) != expected_fields:
            raise ValueError("Quality metric field count is invalid")
        if not parameters <= {"metric", "minimum", "maximum"} or not (
            {"minimum", "maximum"} & parameters
        ):
            raise ValueError("Quality metric boundaries are invalid")
        try:
            for key in {"minimum", "maximum"} & parameters:
                value = Decimal(rule.parameters[key])
                if not value.is_finite():
                    raise InvalidOperation
        except InvalidOperation as error:
            raise ValueError("Quality metric boundaries must be finite decimals") from error
        if rule.outcome not in {QualityOutcomePolicy.WARNING, QualityOutcomePolicy.BLOCK}:
            raise ValueError("Dataset metrics may only warn or block")
    elif family is QualityRuleFamily.IQR_OUTLIER:
        if len(rule.input_fields) != 1 or parameters != {"multiplier"}:
            raise ValueError("IQR checks require one field and one multiplier")
        try:
            multiplier = Decimal(rule.parameters["multiplier"])
        except InvalidOperation as error:
            raise ValueError("IQR multiplier must be numeric") from error
        if not multiplier.is_finite() or multiplier <= 0 or multiplier > 10:
            raise ValueError("IQR multiplier is outside the supported bound")


def _advanced_row_rule_failed(
    rule: QualityRule,
    row: CanonicalRow,
    reference_bundle: ReferenceBundle | None,
) -> tuple[bool, str]:
    values = tuple(row.proposed_values.get(field) for field in rule.input_fields)
    if any(value is None or value == "" for value in values):
        return False, ""
    if rule.family is QualityRuleFamily.LUHN_CHECKSUM:
        digits = "".join(character for character in str(values[0]) if character.isdigit())
        if not digits or any(
            not (character.isdigit() or character in " -")
            for character in str(values[0])
        ):
            return True, "The value is not a valid Luhn-checksum input."
        total = 0
        parity = len(digits) % 2
        for index, character in enumerate(digits):
            number = int(character)
            if index % 2 == parity:
                number *= 2
                if number > 9:
                    number -= 9
            total += number
        return total % 10 != 0, "The value fails the configured Luhn checksum."
    if rule.family is QualityRuleFamily.IBAN_MOD97:
        compact = "".join(str(values[0]).split()).upper()
        if (
            not 15 <= len(compact) <= 34
            or not compact[:2].isalpha()
            or not compact[2:4].isdigit()
            or not compact.isalnum()
        ):
            return True, "The value is not valid IBAN syntax."
        rearranged = compact[4:] + compact[:4]
        remainder = 0
        for character in rearranged:
            token = character if character.isdigit() else str(ord(character) - 55)
            for digit in token:
                remainder = (remainder * 10 + int(digit)) % 97
        return remainder != 1, "The value fails the IBAN MOD-97 checksum."
    if rule.family is QualityRuleFamily.POSTAL_FORMAT:
        jurisdiction = rule.parameters["jurisdiction"].upper()
        value = str(values[0]).strip().upper().replace(" ", "")
        valid = {
            "BE": len(value) == 4 and value.isdigit(),
            "DE": len(value) == 5 and value.isdigit(),
            "FR": len(value) == 5 and value.isdigit(),
            "LU": len(value) == 4 and value.isdigit(),
            "NL": len(value) == 6 and value[:4].isdigit() and value[4:].isalpha(),
            "GB": 5 <= len(value) <= 7 and value.isalnum(),
        }[jurisdiction]
        return not valid, f"The value does not match the approved {jurisdiction} postal syntax."
    if rule.family is QualityRuleFamily.DATE_WINDOW:
        value = values[0]
        try:
            parsed = value if isinstance(value, date) else date.fromisoformat(str(value))
        except ValueError:
            return True, "The value is not an ISO date for the approved date-window check."
        failed = (
            "minimum" in rule.parameters
            and parsed < date.fromisoformat(rule.parameters["minimum"])
        ) or (
            "maximum" in rule.parameters
            and parsed > date.fromisoformat(rule.parameters["maximum"])
        )
        return failed, "The date is outside the approved window."
    if rule.family is QualityRuleFamily.APPROVED_CODE_LIST:
        dataset = next(
            (
                item
                for item in (reference_bundle.datasets if reference_bundle else ())
                if item.reference_id == rule.parameters["reference_id"]
                and item.content_hash == rule.parameters["reference_content_hash"]
            ),
            None,
        )
        if dataset is None:
            raise QualityError("The approved code list is missing or has changed")
        return dataset.lookup(values) is None, "The value is absent from the approved code list."
    raise QualityError("An approved row check uses an unsupported family")


def _finite_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _metric_value(rule: QualityRule, rows: Iterable[CanonicalRow]) -> Decimal:
    items = tuple(rows)
    metric = rule.parameters["metric"]
    if metric == "count":
        return Decimal(len(items))
    values = tuple(item.proposed_values.get(rule.input_fields[0]) for item in items)
    present = tuple(item for item in values if item is not None and item != "")
    if metric == "distinct_count":
        return Decimal(len({canonical_json_bytes(portable_value(item)) for item in present}))
    if metric == "null_rate":
        return Decimal(sum(item is None or item == "" for item in values)) / Decimal(len(values) or 1)
    if metric == "duplicate_rate":
        distinct = len({canonical_json_bytes(portable_value(item)) for item in present})
        return Decimal(max(len(present) - distinct, 0)) / Decimal(len(present) or 1)
    numeric = tuple(item for value in present if (item := _finite_decimal(value)) is not None)
    if not numeric:
        return Decimal(0)
    if metric == "minimum":
        return min(numeric)
    if metric == "maximum":
        return max(numeric)
    if metric == "sum":
        return sum(numeric, Decimal(0))
    raise QualityError("An approved dataset metric is unsupported")


def _quartile(values: list[Decimal], fraction: Decimal) -> Decimal:
    position = fraction * Decimal(len(values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - Decimal(lower)
    return values[lower] + (values[upper] - values[lower]) * weight


def _run_quality_issue(
    workspace_state: WorkspaceState,
    rule: QualityRule,
    reason_code: str,
    message: str,
) -> QualityIssue:
    return QualityIssue(
        issue_id=_hash({
            "rule_id": rule.rule_id,
            "reason_code": reason_code,
            "dataset": rule.dataset,
            "message": message,
            "policy": rule.outcome.value,
        }),
        rule_id=rule.rule_id,
        family=rule.family,
        reason_code=reason_code,
        message=message,
        dataset=rule.dataset,
        row_id=None,
        source_row=None,
        affected_fields=tuple(sorted(rule.input_fields)),
        policy=rule.outcome,
        owner_role=rule.owner_role,
        owner_label=_owner_label(workspace_state, rule.owner_role),
        evidence_display=rule.evidence_display,
    )


def _logical_references(row: CanonicalRow) -> tuple[LogicalReference, ...]:
    found: list[LogicalReference] = []

    def collect(value: object) -> None:
        if isinstance(value, LogicalReference):
            found.append(value)
        elif isinstance(value, tuple):
            for item in value:
                collect(item)

    for value in (*row.target_identity, *row.target_scope, *row.references.values()):
        collect(value)
    return tuple(found)


def _identity_logical_references(row: CanonicalRow) -> tuple[LogicalReference, ...]:
    """Return incoming references that define a record's target identity.

    A child record that cannot be prepared safely must also set aside the
    parent record it uses as part of its identity. Ordinary relationship fields
    remain one-way dependencies and therefore do not set aside lookup records.
    """

    found: list[LogicalReference] = []

    def collect(value: object) -> None:
        if isinstance(value, LogicalReference):
            found.append(value)
        elif isinstance(value, tuple):
            for item in value:
                collect(item)

    for value in (*row.target_identity, *row.target_scope):
        collect(value)
    return tuple(found)


def _owner_label(workspace_state: WorkspaceState, role: QualityOwnerRole) -> str:
    if role is QualityOwnerRole.FUNCTIONAL_OWNER:
        return "Functional owner"
    return "Data manager"


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


def _rule_id(workspace_id: str, dataset: str, family: QualityRuleFamily, name: str, *extra: object) -> str:
    return _hash({"workspace_id": workspace_id, "dataset": dataset, "family": family.value, "name": name, "extra": portable_value(extra)})


def _hash(payload: object) -> str:
    return "sha256:" + sha256(canonical_json_bytes(payload)).hexdigest()


def _require_hash(value: str, label: str) -> None:
    if len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError(f"{label} must be a canonical sha256 hash")
    try:
        int(value[7:], 16)
    except ValueError as error:
        raise ValueError(f"{label} must be a canonical sha256 hash") from error
