"""Deterministic review evidence for prepared values.

Normalization review is target-independent.  It groups the exact values
already produced by canonical staging, records manager decisions, and freezes
the eligible dataset without contacting or authorizing Odoo.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Iterable, Mapping

from .governance import (
    ApprovalMode,
    CorrectionGroupKey,
    CorrectionImpact,
    DryRun,
    DryRunSummary,
)
from .domain.mapping.contracts import DatasetMapping
from .domain.serialization import CanonicalJsonObjectHasher
from .models import canonical_json_bytes
from .projects import DataClassification, MigrationProject
from .quality import (
    QualityOutcomePolicy,
    QualityRun,
    StoredQualityRun,
    retention_context_hash,
)
from .domain.resolution import EffectiveDataset
from .domain.staging.preparation_session import StoredCanonicalStagingRun
from .staging_contracts import CanonicalStagingRun


NORMALIZATION_CONTRACT_VERSION = 2
NORMALIZATION_EVALUATOR_VERSION = 2
_SUPPORTED_NORMALIZATION_VERSIONS = {(1, 1), (2, 2)}
NORMALIZATION_POLICY_VERSION = 1
NORMALIZATION_EXAMPLE_LIMIT = 5


class NormalizationError(ValueError):
    """Raised when prepared-value review evidence is unsafe or stale."""


class NormalizationOutcome(StrEnum):
    """Review policy assigned to a group of prepared-value effects."""

    AUTOMATIC = "AUTOMATIC"
    DECISION_REQUIRED = "DECISION_REQUIRED"
    REVIEW_FINDING = "REVIEW_FINDING"
    TARGET_SUPPLIED = "TARGET_SUPPLIED"


class NormalizationGroupKind(StrEnum):
    """Whether a review group describes a change or a quality warning."""

    CHANGE = "CHANGE"
    FINDING = "FINDING"


@dataclass(frozen=True, slots=True)
class NormalizationCandidate:
    """One display-oriented before/after effect from canonical evaluation."""

    dataset: str
    source_row: int
    source_label: str
    target_field: str
    raw_display: str
    proposed_display: str
    rules: str
    outcome: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class NormalizationExample:
    """Bounded before/after example displayed for one review group."""

    source_row: int
    before: str
    after: str

    def to_portable_dict(self) -> dict[str, Any]:
        """Serialize the example without exposing additional row data."""

        return {
            "source_row": self.source_row,
            "before": self.before,
            "after": self.after,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NormalizationExample":
        """Reconstruct a display example from persisted evidence."""

        return cls(
            source_row=int(payload["source_row"]),
            before=str(payload["before"]),
            after=str(payload["after"]),
        )


@dataclass(frozen=True, slots=True)
class NormalizationEffect:
    """One canonical row/field value effect linked to a review group."""

    effect_id: str
    group_id: str
    row_id: str
    dataset: str
    source_row: int
    target_field: str
    before: str
    after: str
    eligible: bool

    def __post_init__(self) -> None:
        for value, label in (
            (self.effect_id, "normalization effect ID"),
            (self.group_id, "normalization group ID"),
            (self.row_id, "normalization row ID"),
        ):
            _require_hash(value, label)
        if not self.dataset or not self.target_field or self.source_row < 1:
            raise ValueError("Normalization effect coordinates are invalid")

    def to_portable_dict(self) -> dict[str, Any]:
        """Serialize the stable effect identity and protected display values."""

        return {
            "effect_id": self.effect_id,
            "group_id": self.group_id,
            "row_id": self.row_id,
            "dataset": self.dataset,
            "source_row": self.source_row,
            "target_field": self.target_field,
            "before": self.before,
            "after": self.after,
            "eligible": self.eligible,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NormalizationEffect":
        """Reconstruct a validated effect from persisted evidence."""

        return cls(
            effect_id=str(payload["effect_id"]),
            group_id=str(payload["group_id"]),
            row_id=str(payload["row_id"]),
            dataset=str(payload["dataset"]),
            source_row=int(payload["source_row"]),
            target_field=str(payload["target_field"]),
            before=str(payload["before"]),
            after=str(payload["after"]),
            eligible=bool(payload["eligible"]),
        )


@dataclass(frozen=True, slots=True)
class NormalizationReviewGroup:
    """Aggregate effects that share one rule and one governance decision.

    Groups keep the UI and governance model bounded: reviewers approve a
    stable semantic rule group rather than making one decision per record.
    ``decision_key`` connects this object to :class:`governance.DryRun`.
    """

    group_id: str
    rule_id: str
    kind: NormalizationGroupKind
    outcome: NormalizationOutcome
    dataset: str
    dataset_label: str
    target_field: str
    field_label: str
    name: str
    explanation: str
    owner_label: str
    eligible_count: int
    set_aside_count: int
    examples: tuple[NormalizationExample, ...] = ()

    def __post_init__(self) -> None:
        _require_hash(self.group_id, "normalization group ID")
        _require_hash(self.rule_id, "normalization rule ID")
        if not all(
            (
                self.dataset,
                self.dataset_label,
                self.target_field,
                self.field_label,
                self.name,
                self.explanation,
                self.owner_label,
            )
        ):
            raise ValueError("Normalization review group is incomplete")
        if self.eligible_count < 0 or self.set_aside_count < 0:
            raise ValueError("Normalization group counts cannot be negative")
        if not self.eligible_count and not self.set_aside_count:
            raise ValueError("Normalization groups require affected records")
        if len(self.examples) > NORMALIZATION_EXAMPLE_LIMIT:
            raise ValueError("Normalization examples exceed the display limit")

    @property
    def requires_decision(self) -> bool:
        """Whether eligible records in this group need an explicit decision."""

        return self.eligible_count > 0 and self.outcome in {
            NormalizationOutcome.DECISION_REQUIRED,
            NormalizationOutcome.REVIEW_FINDING,
        }

    @property
    def decision_key(self) -> CorrectionGroupKey:
        """Return the stable key used to store this group's governance choice."""

        return CorrectionGroupKey(
            rule_id=self.rule_id,
            dataset=self.dataset,
            # Include this group's stable identity so two findings for the
            # same visible field cannot accidentally share one decision.
            field=f"{self.target_field}:{self.group_id[7:19]}",
        )

    def to_portable_dict(self) -> dict[str, Any]:
        """Serialize group metadata, counts, and bounded examples."""

        return {
            "group_id": self.group_id,
            "rule_id": self.rule_id,
            "kind": self.kind.value,
            "outcome": self.outcome.value,
            "dataset": self.dataset,
            "dataset_label": self.dataset_label,
            "target_field": self.target_field,
            "field_label": self.field_label,
            "name": self.name,
            "explanation": self.explanation,
            "owner_label": self.owner_label,
            "eligible_count": self.eligible_count,
            "set_aside_count": self.set_aside_count,
            "examples": [item.to_portable_dict() for item in self.examples],
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
    ) -> "NormalizationReviewGroup":
        """Reconstruct and validate a review group from storage."""

        return cls(
            group_id=str(payload["group_id"]),
            rule_id=str(payload["rule_id"]),
            kind=NormalizationGroupKind(str(payload["kind"])),
            outcome=NormalizationOutcome(str(payload["outcome"])),
            dataset=str(payload["dataset"]),
            dataset_label=str(payload["dataset_label"]),
            target_field=str(payload["target_field"]),
            field_label=str(payload["field_label"]),
            name=str(payload["name"]),
            explanation=str(payload["explanation"]),
            owner_label=str(payload["owner_label"]),
            eligible_count=int(payload["eligible_count"]),
            set_aside_count=int(payload["set_aside_count"]),
            examples=tuple(
                NormalizationExample.from_dict(item)
                for item in payload.get("examples", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class NormalizationEvaluation:
    """Complete Stage-G review input bound to staging and quality evidence.

    Effects explain individual changes; groups define the decisions reviewers
    make. ``eligible_dataset_hash`` fingerprints the exact canonical rows kept
    by quality and is frozen only after required group decisions are resolved.
    """

    project_id: str
    staging_content_hash: str
    quality_content_hash: str
    mapping_hash: str
    schema_hash: str
    policy_hash: str
    retention_context_hash: str
    eligible_dataset_hash: str
    effects: tuple[NormalizationEffect, ...]
    groups: tuple[NormalizationReviewGroup, ...]
    effective_dataset_hash: str | None = None
    evaluator_version: int = NORMALIZATION_EVALUATOR_VERSION
    contract_version: int = NORMALIZATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (self.contract_version, self.evaluator_version) not in _SUPPORTED_NORMALIZATION_VERSIONS:
            raise ValueError("Normalization evidence version is unsupported")
        if self.contract_version >= 2:
            if self.effective_dataset_hash is None:
                raise ValueError("Current normalization evidence requires resolved data")
            _require_hash(
                self.effective_dataset_hash,
                "normalization effective-dataset hash",
            )
        elif self.effective_dataset_hash is not None:
            raise ValueError("Legacy normalization evidence cannot bind resolved data")
        if not self.project_id:
            raise ValueError("Normalization evidence requires a project")
        for value, label in (
            (self.staging_content_hash, "normalization staging hash"),
            (self.quality_content_hash, "normalization quality hash"),
            (self.mapping_hash, "normalization mapping hash"),
            (self.schema_hash, "normalization schema hash"),
            (self.policy_hash, "normalization policy hash"),
            (self.retention_context_hash, "normalization retention hash"),
            (self.eligible_dataset_hash, "eligible dataset hash"),
        ):
            _require_hash(value, label)
        if self.effects != tuple(sorted(self.effects, key=lambda item: item.effect_id)):
            raise ValueError("Normalization effects must be deterministically ordered")
        if len({item.effect_id for item in self.effects}) != len(self.effects):
            raise ValueError("Normalization effects must be unique")
        if self.groups != tuple(sorted(self.groups, key=lambda item: item.group_id)):
            raise ValueError("Normalization groups must be deterministically ordered")
        group_ids = {item.group_id for item in self.groups}
        if len(group_ids) != len(self.groups):
            raise ValueError("Normalization groups must be unique")
        if {item.group_id for item in self.effects} - group_ids:
            raise ValueError("Normalization effects point to missing groups")

    @property
    def content_hash(self) -> str:
        """Hash the immutable evaluation consumed by the normalization run."""

        return _hash(self.to_portable_dict(include_hash=False))

    @property
    def ready_count(self) -> int:
        """Count low-risk groups governed by the automatic policy."""

        return sum(
            1
            for group in self.groups
            if group.outcome is NormalizationOutcome.AUTOMATIC
        )

    @property
    def pending_group_count(self) -> int:
        """Count groups that require explicit reviewer decisions."""

        return sum(group.requires_decision for group in self.groups)

    @property
    def changed_record_count(self) -> int:
        """Count distinct eligible records with at least one visible effect."""

        return len({item.row_id for item in self.effects if item.eligible})

    def to_portable_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        """Serialize the evaluation and all review evidence."""

        payload = {
            "contract_version": self.contract_version,
            "evaluator_version": self.evaluator_version,
            "project_id": self.project_id,
            "staging_content_hash": self.staging_content_hash,
            "quality_content_hash": self.quality_content_hash,
            "mapping_hash": self.mapping_hash,
            "schema_hash": self.schema_hash,
            "policy_hash": self.policy_hash,
            "retention_context_hash": self.retention_context_hash,
            "eligible_dataset_hash": self.eligible_dataset_hash,
            "effects": [item.to_portable_dict() for item in self.effects],
            "groups": [item.to_portable_dict() for item in self.groups],
        }
        if self.contract_version >= 2:
            payload["effective_dataset_hash"] = self.effective_dataset_hash
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def to_json(self) -> str:
        """Return the evaluation as canonical JSON."""

        return canonical_json_bytes(self.to_portable_dict()).decode("utf-8")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NormalizationEvaluation":
        """Load an evaluation and verify its persisted content hash."""

        evaluation = cls(
            contract_version=int(payload.get("contract_version", 0)),
            evaluator_version=int(payload.get("evaluator_version", 0)),
            project_id=str(payload["project_id"]),
            staging_content_hash=str(payload["staging_content_hash"]),
            quality_content_hash=str(payload["quality_content_hash"]),
            mapping_hash=str(payload["mapping_hash"]),
            schema_hash=str(payload["schema_hash"]),
            policy_hash=str(payload["policy_hash"]),
            retention_context_hash=str(payload["retention_context_hash"]),
            eligible_dataset_hash=str(payload["eligible_dataset_hash"]),
            effects=tuple(
                NormalizationEffect.from_dict(item)
                for item in payload.get("effects", ())
            ),
            groups=tuple(
                NormalizationReviewGroup.from_dict(item)
                for item in payload.get("groups", ())
            ),
            effective_dataset_hash=(
                str(payload["effective_dataset_hash"])
                if payload.get("effective_dataset_hash") is not None
                else None
            ),
        )
        if payload.get("content_hash") != evaluation.content_hash:
            raise ValueError("Normalization content hash is invalid")
        return evaluation

    @classmethod
    def from_json(cls, value: str) -> "NormalizationEvaluation":
        """Load and validate an evaluation from canonical JSON."""

        return cls.from_dict(json.loads(value))


@dataclass(frozen=True, slots=True)
class StoredNormalizationEvaluation:
    """Validated Stage-G header backed by a replayable bounded effect stream."""

    project_id: str
    staging_content_hash: str
    quality_content_hash: str
    mapping_hash: str
    schema_hash: str
    policy_hash: str
    retention_context_hash: str
    eligible_dataset_hash: str
    effects: Iterable[NormalizationEffect]
    groups: tuple[NormalizationReviewGroup, ...]
    effect_count: int
    changed_record_count: int
    effective_dataset_hash: str
    evaluator_version: int = NORMALIZATION_EVALUATOR_VERSION
    contract_version: int = NORMALIZATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (self.contract_version, self.evaluator_version) not in _SUPPORTED_NORMALIZATION_VERSIONS:
            raise ValueError("Normalization evidence version is unsupported")
        if not self.project_id or self.effect_count < 0 or self.changed_record_count < 0:
            raise ValueError("Stored normalization evidence is incomplete")
        for value, label in (
            (self.staging_content_hash, "normalization staging hash"),
            (self.quality_content_hash, "normalization quality hash"),
            (self.mapping_hash, "normalization mapping hash"),
            (self.schema_hash, "normalization schema hash"),
            (self.policy_hash, "normalization policy hash"),
            (self.retention_context_hash, "normalization retention hash"),
            (self.eligible_dataset_hash, "eligible dataset hash"),
            (self.effective_dataset_hash, "normalization effective-dataset hash"),
        ):
            _require_hash(value, label)
        if self.groups != tuple(sorted(self.groups, key=lambda item: item.group_id)):
            raise ValueError("Normalization groups must be deterministically ordered")
        if len({item.group_id for item in self.groups}) != len(self.groups):
            raise ValueError("Normalization groups must be unique")
        if self.changed_record_count > self.effect_count:
            raise ValueError("Changed records exceed normalization effects")

    @property
    def ready_count(self) -> int:
        return sum(
            group.outcome is NormalizationOutcome.AUTOMATIC
            for group in self.groups
        )

    @property
    def pending_group_count(self) -> int:
        return sum(group.requires_decision for group in self.groups)


@dataclass(frozen=True, slots=True)
class NormalizationRunSummary:
    """Lifecycle and count projection for a durable Stage-G review run."""

    run_id: str
    project_id: str
    content_hash: str
    staging_run_id: str
    staging_content_hash: str
    quality_run_id: str
    quality_content_hash: str
    eligible_dataset_hash: str
    status: str
    lifecycle_version: int
    published_at: datetime
    published_by: str
    eligible_record_count: int
    changed_record_count: int
    automatic_group_count: int
    decision_group_count: int
    reviewed_group_count: int
    set_aside_record_count: int
    effective_dataset_run_id: str | None = None
    effective_dataset_hash: str | None = None

    @property
    def frozen(self) -> bool:
        """Whether approval fixed the eligible dataset for later stages."""

        return self.status == "FROZEN"

    @property
    def decisions_left(self) -> int:
        """Count required review groups without a recorded decision."""

        return max(0, self.decision_group_count - self.reviewed_group_count)


@dataclass(frozen=True, slots=True)
class NormalizationReviewPage:
    """Bounded page of normalization groups for review navigation."""

    groups: tuple[NormalizationReviewGroup, ...]
    matching_count: int
    page: int
    page_count: int


def evaluate_normalization(
    *,
    project: MigrationProject,
    staging: CanonicalStagingRun | StoredCanonicalStagingRun,
    quality: QualityRun | StoredQualityRun,
    mappings: Mapping[str, DatasetMapping],
    candidates: Iterable[NormalizationCandidate],
    published_staging_content_hash: str | None = None,
    published_quality_content_hash: str | None = None,
    effective: EffectiveDataset | None = None,
) -> NormalizationEvaluation:
    """Build complete review groups without repository or Odoo access."""

    if staging.project_id != project.project_id or quality.project_id != project.project_id:
        raise NormalizationError("Prepared review evidence belongs to another project")
    staging_content_hash = (
        published_staging_content_hash or staging.content_hash
    )
    if quality.staging_content_hash != staging_content_hash:
        raise NormalizationError("Prepared review no longer matches the data checks")
    if quality.mapping_hash != staging.mapping_hash or quality.schema_hash != staging.schema_hash:
        raise NormalizationError("Prepared review no longer matches the field matches")
    effective_dataset_hash = (
        effective.content_hash if effective is not None else staging_content_hash
    )
    if quality.effective_dataset_hash != effective_dataset_hash:
        raise NormalizationError("Prepared review no longer matches resolved data")
    if effective is not None and (
        effective.project_id != project.project_id
        or effective.staging_content_hash != staging_content_hash
    ):
        raise NormalizationError("Resolved data no longer matches prepared data")
    if not quality.can_compare:
        raise NormalizationError("Fix the data-check setup before reviewing prepared data")
    if not all(item.passed for item in staging.control_totals):
        raise NormalizationError("Resolve the known totals before reviewing prepared data")

    rows_by_coordinate: dict[tuple[str, int], list[Any]] = {}
    effective_rows_by_id: dict[str, Any]
    effective_id_by_source: dict[str, str]
    if effective is None:
        # A stored staging sequence decodes each iteration. Build all indexes
        # together so they share one canonical row object instead of retaining
        # multiple complete decoded copies of the same evidence.
        effective_rows_by_id = {}
        effective_id_by_source = {}
        for row in staging.rows:
            rows_by_coordinate.setdefault(
                (row.dataset, row.source_row),
                [],
            ).append(row)
            effective_rows_by_id[row.row_id] = row
            effective_id_by_source[row.row_id] = row.row_id
    else:
        for row in staging.rows:
            rows_by_coordinate.setdefault(
                (row.dataset, row.source_row),
                [],
            ).append(row)
        effective_rows_by_id = {
            item.row_id: item.canonical_row for item in effective.rows
        }
        effective_id_by_source = {
            item.source_row_id: item.effective_row_id
            for item in effective.accounting
        }
    eligible_ids = quality.eligible_row_ids
    policy_hash = _hash(
        {
            "policy_version": NORMALIZATION_POLICY_VERSION,
            "mapping_hash": staging.mapping_hash,
            "rules": _policy_manifest(mappings),
        }
    )
    effect_rows: list[NormalizationEffect] = []
    effect_ids: set[str] = set()
    group_metadata: dict[str, dict[str, Any]] = {}
    restricted = project.data_classification is DataClassification.RESTRICTED

    for candidate in candidates:
        if candidate.outcome == "invalid":
            continue
        matches = rows_by_coordinate.get((candidate.dataset, candidate.source_row), ())
        if len(matches) != 1:
            raise NormalizationError(
                "Prepared changes cannot be matched safely to one business record"
            )
        source_row = matches[0]
        effective_row_id = effective_id_by_source.get(source_row.row_id)
        row = effective_rows_by_id.get(effective_row_id or "")
        if row is None:
            raise NormalizationError(
                "Prepared changes no longer match the resolved business records"
            )
        mapping = mappings.get(candidate.dataset)
        if mapping is None:
            raise NormalizationError("Prepared changes use an unknown table")
        identity_fields = {
            field
            for component in (*mapping.target_identity, *mapping.target_scope)
            for field in component.target_fields
        }
        identity_fields.update(item.target_field for item in mapping.relationships)
        outcome = _review_outcome(
            candidate,
            identity_impact=candidate.target_field in identity_fields,
        )
        rule_id = _hash(
            {
                "mapping_hash": staging.mapping_hash,
                "dataset": candidate.dataset,
                "target_field": candidate.target_field,
                "rules": candidate.rules,
                "outcome": outcome.value,
            }
        )
        group_id = _hash(
            {
                "kind": NormalizationGroupKind.CHANGE.value,
                "rule_id": rule_id,
                "dataset": candidate.dataset,
                "field": candidate.target_field,
            }
        )
        before = _protected_display(candidate.raw_display, restricted=restricted)
        after = _protected_display(candidate.proposed_display, restricted=restricted)
        effect = NormalizationEffect(
            effect_id=_hash(
                {
                    "group_id": group_id,
                    "row_id": row.row_id,
                    "before": before,
                    "after": after,
                }
            ),
            group_id=group_id,
            row_id=row.row_id,
            dataset=row.dataset,
            source_row=row.source_row,
            target_field=candidate.target_field,
            before=before,
            after=after,
            eligible=row.row_id in eligible_ids,
        )
        if effect.effect_id not in effect_ids:
            effect_rows.append(effect)
            effect_ids.add(effect.effect_id)
        name, explanation = _change_language(candidate.rules, outcome)
        group_metadata.setdefault(
            group_id,
            {
                "rule_id": rule_id,
                "kind": NormalizationGroupKind.CHANGE,
                "outcome": outcome,
                "dataset": candidate.dataset,
                "target_field": candidate.target_field,
                "name": name,
                "explanation": explanation,
                "owner_label": project.data_manager or "Data manager",
            },
        )

    warning_rows: dict[str, list[Any]] = {}
    warning_metadata: dict[str, dict[str, Any]] = {}
    for issue in quality.issues:
        if (
            issue.policy is not QualityOutcomePolicy.WARNING
            or issue.row_id is None
            or issue.row_id not in eligible_ids
        ):
            continue
        target_field = issue.affected_fields[0] if issue.affected_fields else "review"
        group_id = _hash(
            {
                "kind": NormalizationGroupKind.FINDING.value,
                "rule_id": issue.rule_id,
                "dataset": issue.dataset,
                "field": target_field,
                "reason": issue.reason_code,
            }
        )
        warning_rows.setdefault(group_id, []).append(issue)
        warning_metadata.setdefault(
            group_id,
            {
                "rule_id": issue.rule_id,
                "kind": NormalizationGroupKind.FINDING,
                "outcome": NormalizationOutcome.REVIEW_FINDING,
                "dataset": issue.dataset,
                "target_field": target_field,
                "name": "Review this data finding",
                "explanation": issue.message,
                "owner_label": issue.owner_label or project.data_manager or "Data manager",
            },
        )

    effects_by_group: dict[str, list[NormalizationEffect]] = {}
    for effect in effect_rows:
        effects_by_group.setdefault(effect.group_id, []).append(effect)
    groups: list[NormalizationReviewGroup] = []
    for group_id, metadata in sorted(group_metadata.items()):
        items = sorted(
            effects_by_group[group_id],
            key=lambda item: (item.source_row, item.row_id, item.effect_id),
        )
        examples = tuple(
            NormalizationExample(item.source_row, item.before, item.after)
            for item in items
            if item.eligible
        )[:NORMALIZATION_EXAMPLE_LIMIT]
        groups.append(
            NormalizationReviewGroup(
                group_id=group_id,
                dataset_label=_human_label(str(metadata["dataset"])),
                field_label=_human_label(str(metadata["target_field"])),
                eligible_count=sum(item.eligible for item in items),
                set_aside_count=sum(not item.eligible for item in items),
                examples=examples,
                **metadata,
            )
        )
    for group_id, metadata in sorted(warning_metadata.items()):
        issues = warning_rows[group_id]
        groups.append(
            NormalizationReviewGroup(
                group_id=group_id,
                dataset_label=_human_label(str(metadata["dataset"])),
                field_label=_human_label(str(metadata["target_field"])),
                eligible_count=len({item.row_id for item in issues}),
                set_aside_count=0,
                examples=(),
                **metadata,
            )
        )

    quality_content_hash = (
        published_quality_content_hash or quality.content_hash
    )
    eligible_dataset_hash = canonical_eligible_dataset_hash(
        staging,
        quality,
        staging_content_hash=staging_content_hash,
        quality_content_hash=quality_content_hash,
        effective=effective,
    )
    return NormalizationEvaluation(
        project_id=project.project_id,
        staging_content_hash=staging_content_hash,
        quality_content_hash=quality_content_hash,
        mapping_hash=staging.mapping_hash,
        schema_hash=staging.schema_hash,
        policy_hash=policy_hash,
        retention_context_hash=retention_context_hash(project),
        eligible_dataset_hash=eligible_dataset_hash,
        effects=tuple(sorted(effect_rows, key=lambda item: item.effect_id)),
        groups=tuple(sorted(groups, key=lambda item: item.group_id)),
        effective_dataset_hash=effective_dataset_hash,
    )


def start_dry_run(
    evaluation: NormalizationEvaluation | StoredNormalizationEvaluation,
    *,
    run_id: str,
    source_hashes: Mapping[str, str],
) -> DryRun:
    """Translate normalization groups into the shared decision state machine.

    Automatic groups become non-blocking correction impacts; groups requiring
    judgment must be approved or rejected before :class:`DryRun` can be
    approved and frozen with ``evaluation.eligible_dataset_hash``.
    """

    corrections = tuple(
        sorted(
            (
                CorrectionImpact(
                    key=group.decision_key,
                    approval_mode=(
                        ApprovalMode.REQUIRED
                        if group.requires_decision
                        else ApprovalMode.AUTOMATIC
                    ),
                    affected_count=group.eligible_count,
                )
                for group in evaluation.groups
                if group.eligible_count > 0
                and group.outcome is not NormalizationOutcome.TARGET_SUPPLIED
            ),
            key=lambda item: item.key,
        )
    )
    return DryRun(
        run_id=run_id,
        source_hashes=source_hashes,
        ruleset_hash=evaluation.policy_hash,
    ).complete(DryRunSummary(corrections=corrections))


def _review_outcome(
    candidate: NormalizationCandidate,
    *,
    identity_impact: bool,
) -> NormalizationOutcome:
    if identity_impact:
        return NormalizationOutcome.DECISION_REQUIRED
    rules = candidate.rules.casefold()
    required_markers = (
        "constant",
        "fallback",
        "match ",
        "formula",
        "find and replace",
        "case:",
        "empty to null",
        "round to",
        "reviewed value match",
    )
    if any(marker in rules for marker in required_markers):
        return NormalizationOutcome.DECISION_REQUIRED
    allowed_markers = ("source", "trim", "collapse spaces", "parse ")
    if all(
        any(part.casefold().startswith(marker) for marker in allowed_markers)
        for part in (item.strip() for item in candidate.rules.split("+"))
    ):
        return NormalizationOutcome.AUTOMATIC
    raise NormalizationError(
        "A prepared change has no safe review policy. Update the field match."
    )


def _change_language(
    rules: str,
    outcome: NormalizationOutcome,
) -> tuple[str, str]:
    lowered = rules.casefold()
    if "match " in lowered or "reviewed value match" in lowered:
        return "Use your reviewed value matches", "Impodo replaced source choices with the business values you confirmed."
    if "fallback" in lowered:
        return "Fill values using your fallback", "Impodo used the fallback you confirmed where the source value was empty."
    if "constant" in lowered:
        return "Use the value you supplied", "Impodo applied the same confirmed value to the affected records."
    if "formula" in lowered:
        return "Apply your prepared calculation", "Impodo calculated these values using the field rule you confirmed."
    if "find and replace" in lowered:
        return "Replace source text", "Impodo applied the replacement you confirmed."
    if "case:" in lowered:
        return "Standardize letter case", "Impodo changed capitalization using the rule you confirmed."
    if "round to" in lowered:
        return "Round prepared numbers", "Impodo rounded these values to the precision you confirmed."
    if "empty to null" in lowered:
        return "Treat empty values as blank", "Impodo converted empty source values into prepared blank values."
    if "trim" in lowered or "collapse spaces" in lowered:
        return "Remove extra spaces", "Impodo removed leading, trailing, or repeated spaces."
    if "parse " in lowered:
        return "Prepare consistent value types", "Impodo converted source text into the confirmed number, date, or true/false format."
    if outcome is NormalizationOutcome.AUTOMATIC:
        return "Apply your confirmed preparation rule", "Impodo applied a low-risk rule already confirmed in Field matching."
    return "Review this prepared change", "Impodo changed the prepared value using a rule you confirmed."


def _policy_manifest(mappings: Mapping[str, DatasetMapping]) -> list[dict[str, Any]]:
    return [
        {
            "dataset": dataset,
            "identity_fields": sorted(
                field
                for component in (*mapping.target_identity, *mapping.target_scope)
                for field in component.target_fields
            ),
            "relationship_fields": sorted(
                item.target_field for item in mapping.relationships
            ),
            "scalar_fields": sorted(
                (
                    {
                        "field": item.target_field,
                        "value_source": item.value_source.value,
                        "transform": asdict(item.transform),
                        "validation": asdict(item.validation),
                        "value_mappings": [
                            {
                                "source_value": value.source_value,
                                "target_value": value.target_value,
                            }
                            for value in item.value_mappings
                        ],
                    }
                    for item in mapping.fields
                ),
                key=lambda item: item["field"],
            ),
        }
        for dataset, mapping in sorted(mappings.items())
    ]


def _protected_display(value: str, *, restricted: bool) -> str:
    if not restricted or value in {"", "—", "Invalid"}:
        return value
    return "Hidden for restricted data"


def _human_label(value: str) -> str:
    return value.replace("_", " ").strip().title() or "Prepared value"


def _hash(value: object) -> str:
    return "sha256:" + sha256(canonical_json_bytes(value)).hexdigest()


def canonical_eligible_dataset_hash(
    staging: CanonicalStagingRun | StoredCanonicalStagingRun,
    quality: QualityRun | StoredQualityRun,
    *,
    staging_content_hash: str | None = None,
    quality_content_hash: str | None = None,
    effective: EffectiveDataset | None = None,
) -> str:
    """Hash the exact quality-eligible canonical rows for durable reuse."""

    canonical_staging_hash = staging_content_hash or staging.content_hash
    canonical_quality_hash = quality_content_hash or quality.content_hash
    if quality.staging_content_hash != canonical_staging_hash:
        raise NormalizationError("Prepared data no longer matches the data checks")
    effective_dataset_hash = (
        effective.content_hash if effective is not None else canonical_staging_hash
    )
    if quality.effective_dataset_hash != effective_dataset_hash:
        raise NormalizationError("Resolved data no longer matches the data checks")
    eligible_ids = quality.eligible_row_ids
    hasher = CanonicalJsonObjectHasher()
    hasher.add_value("effective_dataset_hash", effective_dataset_hash)
    hasher.add_value("quality_content_hash", canonical_quality_hash)
    hasher.start_array("rows")
    rows = (
        (item.canonical_row for item in effective.rows)
        if effective is not None
        else iter(staging.rows)
    )
    for row in rows:
        if row.row_id in eligible_ids:
            hasher.add_encoded_array_item(
                canonical_json_bytes(row.to_portable_dict())
            )
    hasher.end_array()
    hasher.add_value("staging_content_hash", canonical_staging_hash)
    return hasher.finish()


def _require_hash(value: str, label: str) -> None:
    if not value.startswith("sha256:") or len(value) != 71:
        raise ValueError(f"{label} is invalid")
    try:
        int(value[7:], 16)
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
