"""Extracted transformation impact domain behavior."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from typing import (
    Callable,
    Mapping,
)

from ...models import (
    canonical_json_bytes,
    portable_value,
)
from ...staging_contracts import BROWSER_EVALUATOR_VERSION
from ..contracts import (
    TRANSFORMATION_IMPACT_CONTRACT_VERSION,
    TRANSFORMATION_IMPACT_DETAIL_LIMIT,
)
from ..mapping.contracts import ScalarFieldMapping, ScalarValueSource
from ..serialization import content_hash


@dataclass(frozen=True, slots=True)
class TransformationRuleImpact:
    """Complete counts for one configured cleanup or Selection rule fact.

    For ``selection_rule``, ``matched_value_count`` counts every row whose
    conditions matched before priority and ``changed_value_count`` counts rows
    that selected the rule after first-match priority. For
    ``selection_rule_overlap``, both counts contain the rows where that rule
    matched alongside at least one other rule.
    """

    dataset_id: str
    target_field: str
    rule_kind: str
    rule_fingerprint: str
    evaluated_value_count: int = 0
    matched_value_count: int = 0
    changed_value_count: int = 0

    def __post_init__(self) -> None:
        counts = (
            self.evaluated_value_count,
            self.matched_value_count,
            self.changed_value_count,
        )
        if any(value < 0 for value in counts):
            raise ValueError("Transformation rule counts cannot be negative")
        if not (
            self.changed_value_count
            <= self.matched_value_count
            <= self.evaluated_value_count
        ):
            raise ValueError("Transformation rule counts do not reconcile")

    @property
    def requires_acknowledgement(self) -> bool:
        """Return whether current evidence requires an operator decision."""

        if self.rule_kind == "selection_rule":
            return self.matched_value_count == 0
        if self.rule_kind == "selection_rule_overlap":
            return self.matched_value_count > 0

        return self.changed_value_count == 0

    @property
    def acknowledgement_reason(self) -> str | None:
        """Name the exact review decision represented by this fact."""

        if not self.requires_acknowledgement:
            return None
        if self.rule_kind == "selection_rule":
            return "zero_match"
        if self.rule_kind == "selection_rule_overlap":
            return "overlap"
        return "zero_change"


def transformation_rule_impact_definitions(
    dataset_id: str,
    field: ScalarFieldMapping,
) -> tuple[TransformationRuleImpact, ...]:
    """Describe every configured text rule in authored execution order."""

    transform = field.transform
    definitions = []
    for step_index, step in enumerate(transform.text_steps):
        if not step.configured:
            continue
        rule_kind = (
            step.kind
            if step.kind != "find_replace"
            else f"find_replace_{step.search_mode}"
        )
        fingerprint = content_hash(
            {
                "dataset_id": dataset_id,
                "target_field": field.target_field,
                "step_index": step_index,
                "rule_kind": rule_kind,
                "search_value": step.search_value,
                "replacement_value": step.replacement_value,
                "replace_all": step.replace_all,
                "characters": step.characters,
            }
        )
        definitions.append(
            TransformationRuleImpact(
                dataset_id=dataset_id,
                target_field=field.target_field,
                rule_kind=rule_kind,
                rule_fingerprint=fingerprint,
            )
        )
    return tuple(definitions)


def selection_rule_impact_definitions(
    dataset_id: str,
    field: ScalarFieldMapping,
) -> tuple[TransformationRuleImpact, ...]:
    """Describe match and overlap evidence for every ordered Selection rule."""

    if (
        field.value_source is not ScalarValueSource.CONDITIONAL_RULES
        or field.selection_rules is None
    ):
        return ()
    definitions: list[TransformationRuleImpact] = []
    for rule_index, rule in enumerate(field.selection_rules.rules):
        for rule_kind in ("selection_rule", "selection_rule_overlap"):
            definitions.append(
                selection_rule_impact_definition(
                    dataset_id=dataset_id,
                    target_field=field.target_field,
                    rule_index=rule_index,
                    join=rule.join.value,
                    target_value=rule.target_value,
                    conditions=tuple(
                        {
                            "source_column_key": condition.source_column_key,
                            "operator": condition.operator.value,
                            "comparison_value": condition.comparison_value,
                            "value_type": condition.value_type,
                        }
                        for condition in rule.conditions
                    ),
                    rule_kind=rule_kind,
                )
            )
    return tuple(definitions)


def selection_rule_impact_definition(
    *,
    dataset_id: str,
    target_field: str,
    rule_index: int,
    join: str,
    target_value: str,
    conditions: tuple[Mapping[str, object], ...],
    rule_kind: str,
) -> TransformationRuleImpact:
    """Build one stable Selection rule fact from portable rule meaning."""

    if rule_kind not in {"selection_rule", "selection_rule_overlap"}:
        raise ValueError("Selection rule evidence kind is unsupported")
    return TransformationRuleImpact(
        dataset_id=dataset_id,
        target_field=target_field,
        rule_kind=rule_kind,
        rule_fingerprint=content_hash(
            {
                "dataset_id": dataset_id,
                "target_field": target_field,
                "rule_index": rule_index,
                "join": join,
                "target_value": target_value,
                "conditions": [dict(condition) for condition in conditions],
                "evidence_kind": rule_kind,
            }
        ),
    )


def reviewable_rule_impact_definitions(
    dataset_id: str,
    field: ScalarFieldMapping,
) -> tuple[TransformationRuleImpact, ...]:
    """Return every rule fact that the current impact review must publish."""

    return (
        *transformation_rule_impact_definitions(dataset_id, field),
        *selection_rule_impact_definitions(dataset_id, field),
    )


@dataclass(frozen=True, slots=True)
class TransformationImpactRow:
    """One visible raw-to-proposed scalar value change."""

    dataset: str
    source_row: int
    source_column: str
    target_field: str
    raw_value: str
    proposed_value: str
    rules: str
    outcome: str
    message: str = ""


@dataclass(frozen=True, slots=True)
class TransformationImpactReport:
    """Bounded browser projection with complete all-row outcome counts."""

    mapping_content_hash: str
    evaluated_count: int
    changed_count: int
    fallback_count: int
    null_count: int
    invalid_count: int
    provided_count: int
    unchanged_count: int
    rows: tuple[TransformationImpactRow, ...]
    rule_impacts: tuple[TransformationRuleImpact, ...] = ()
    detail_limit: int = TRANSFORMATION_IMPACT_DETAIL_LIMIT

    @property
    def impact_count(self) -> int:
        """Count every evaluated value whose outcome was not unchanged."""

        return (
            self.changed_count
            + self.fallback_count
            + self.null_count
            + self.invalid_count
            + self.provided_count
        )

    @property
    def truncated(self) -> bool:
        """Whether bounded display rows omit additional counted impacts."""

        return self.impact_count > len(self.rows)


@dataclass(frozen=True, slots=True)
class TransformationImpactCounts:
    """Complete outcome accounting for one bounded native result batch."""

    evaluated_count: int = 0
    changed_count: int = 0
    fallback_count: int = 0
    null_count: int = 0
    invalid_count: int = 0
    provided_count: int = 0
    unchanged_count: int = 0

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.evaluated_count,
                self.changed_count,
                self.fallback_count,
                self.null_count,
                self.invalid_count,
                self.provided_count,
                self.unchanged_count,
            )
        ):
            raise ValueError("Transformation outcome counts cannot be negative")
        if self.evaluated_count != (
            self.changed_count
            + self.fallback_count
            + self.null_count
            + self.invalid_count
            + self.provided_count
            + self.unchanged_count
        ):
            raise ValueError("Transformation outcomes do not reconcile")

    @property
    def impact_count(self) -> int:
        return self.evaluated_count - self.unchanged_count


@dataclass(frozen=True, slots=True)
class TransformationImpactIdentity:
    """Hash-bound identity for one reusable transformation-impact snapshot."""

    physical_selection_hash: str
    source_selection_hash: str
    mapping_content_hash: str
    schema_hash: str
    derived_plan_hash: str | None
    contract_version: int = TRANSFORMATION_IMPACT_CONTRACT_VERSION
    evaluator_version: int = BROWSER_EVALUATOR_VERSION

    @property
    def content_hash(self) -> str:
        """Hash every input/version that determines a reusable snapshot."""

        return (
            "sha256:"
            + sha256(
                canonical_json_bytes(
                    {
                        "physical_selection_hash": self.physical_selection_hash,
                        "source_selection_hash": self.source_selection_hash,
                        "mapping_content_hash": self.mapping_content_hash,
                        "schema_hash": self.schema_hash,
                        "derived_plan_hash": self.derived_plan_hash,
                        "contract_version": self.contract_version,
                        "evaluator_version": self.evaluator_version,
                    }
                )
            ).hexdigest()
        )


@dataclass(frozen=True, slots=True)
class TransformationImpactSnapshot:
    """Complete counts for one persisted, filterable impact projection."""

    identity: TransformationImpactIdentity
    created_at: datetime
    created_by: str
    affected_row_count: int
    report: TransformationImpactReport
    acknowledged_rule_fingerprints: tuple[str, ...] = ()

    @property
    def unacknowledged_rule_impacts(self) -> tuple[TransformationRuleImpact, ...]:
        """Return current rule facts that still require explicit review."""

        acknowledged = frozenset(self.acknowledged_rule_fingerprints)
        return tuple(
            item
            for item in self.report.rule_impacts
            if item.requires_acknowledgement
            and item.rule_fingerprint not in acknowledged
        )


@dataclass(frozen=True, slots=True)
class TransformationImpactFilter:
    """Server-side filters shared by the browser table and CSV export."""

    dataset: str = ""
    outcome: str = ""
    target_field: str = ""
    query: str = ""


@dataclass(frozen=True, slots=True)
class TransformationImpactPage:
    """One bounded, deterministically ordered impact-result page."""

    rows: tuple[TransformationImpactRow, ...]
    matching_count: int
    start_position: int
    end_position: int
    previous_before: int | None
    next_after: int | None


@dataclass(slots=True)
class _TransformationImpactCollector:
    mapping_content_hash: str
    detail_limit: int = TRANSFORMATION_IMPACT_DETAIL_LIMIT
    evaluated_count: int = 0
    changed_count: int = 0
    fallback_count: int = 0
    null_count: int = 0
    invalid_count: int = 0
    provided_count: int = 0
    unchanged_count: int = 0
    rows: list[TransformationImpactRow] | None = None
    sink: Callable[[TransformationImpactRow], None] | None = None
    rule_impacts: dict[str, TransformationRuleImpact] | None = None

    def __post_init__(self) -> None:
        if self.rows is None:
            self.rows = []
        if self.rule_impacts is None:
            self.rule_impacts = {}

    def register_rule(self, rule: TransformationRuleImpact) -> None:
        """Register one configured rule even when no row reaches it."""

        assert self.rule_impacts is not None
        existing = self.rule_impacts.get(rule.rule_fingerprint)
        if existing is not None:
            if (
                existing.dataset_id,
                existing.target_field,
                existing.rule_kind,
            ) != (rule.dataset_id, rule.target_field, rule.rule_kind):
                raise ValueError("Transformation rule fingerprint is ambiguous")
            return
        self.rule_impacts[rule.rule_fingerprint] = rule

    def record_rule(
        self,
        rule: TransformationRuleImpact,
        *,
        matched: bool,
        changed: bool,
    ) -> None:
        """Count one non-empty value evaluated by a configured rule."""

        self.register_rule(rule)
        assert self.rule_impacts is not None
        current = self.rule_impacts[rule.rule_fingerprint]
        self.rule_impacts[rule.rule_fingerprint] = TransformationRuleImpact(
            dataset_id=current.dataset_id,
            target_field=current.target_field,
            rule_kind=current.rule_kind,
            rule_fingerprint=current.rule_fingerprint,
            evaluated_value_count=current.evaluated_value_count + 1,
            matched_value_count=current.matched_value_count + int(matched),
            changed_value_count=current.changed_value_count + int(changed),
        )

    def record_rule_precomputed(
        self,
        rule: TransformationRuleImpact,
    ) -> None:
        """Merge complete counts from one bounded native result batch."""

        assert self.rule_impacts is not None
        current = self.rule_impacts.get(rule.rule_fingerprint)
        if current is None:
            self.rule_impacts[rule.rule_fingerprint] = rule
            return
        if (
            current.dataset_id,
            current.target_field,
            current.rule_kind,
        ) != (rule.dataset_id, rule.target_field, rule.rule_kind):
            raise ValueError("Transformation rule fingerprint is ambiguous")
        self.rule_impacts[rule.rule_fingerprint] = TransformationRuleImpact(
            dataset_id=current.dataset_id,
            target_field=current.target_field,
            rule_kind=current.rule_kind,
            rule_fingerprint=current.rule_fingerprint,
            evaluated_value_count=(
                current.evaluated_value_count + rule.evaluated_value_count
            ),
            matched_value_count=(
                current.matched_value_count + rule.matched_value_count
            ),
            changed_value_count=(
                current.changed_value_count + rule.changed_value_count
            ),
        )

    def record(
        self,
        *,
        dataset: str,
        source_row: int,
        source_column: str,
        target_field: str,
        raw_value: object,
        proposed_value: object,
        rules: str,
        outcome: str,
        message: str = "",
    ) -> None:
        self.evaluated_count += 1
        attribute = f"{outcome}_count"
        setattr(self, attribute, getattr(self, attribute) + 1)
        if outcome == "unchanged":
            return
        impact = TransformationImpactRow(
            dataset=dataset,
            source_row=source_row,
            source_column=source_column,
            target_field=target_field,
            raw_value=_display_value(raw_value),
            proposed_value=_display_value(proposed_value),
            rules=rules,
            outcome=outcome,
            message=message,
        )
        if self.sink is not None:
            self.sink(impact)
        if len(self.rows or ()) >= self.detail_limit:
            return
        assert self.rows is not None
        self.rows.append(impact)

    def record_precomputed(
        self,
        counts: TransformationImpactCounts,
        impacts: tuple[TransformationImpactRow, ...],
        rule_impacts: tuple[TransformationRuleImpact, ...] = (),
    ) -> None:
        """Merge one native batch without replaying unchanged scalar cells."""

        if len(impacts) != counts.impact_count:
            raise ValueError("Transformation impact batch does not reconcile")
        actual = {
            outcome: sum(1 for row in impacts if row.outcome == outcome)
            for outcome in (
                "changed",
                "fallback",
                "null",
                "invalid",
                "provided",
            )
        }
        for outcome, expected in (
            ("changed", counts.changed_count),
            ("fallback", counts.fallback_count),
            ("null", counts.null_count),
            ("invalid", counts.invalid_count),
            ("provided", counts.provided_count),
        ):
            if actual[outcome] != expected:
                raise ValueError("Transformation impact outcomes are incomplete")
        self.evaluated_count += counts.evaluated_count
        self.changed_count += counts.changed_count
        self.fallback_count += counts.fallback_count
        self.null_count += counts.null_count
        self.invalid_count += counts.invalid_count
        self.provided_count += counts.provided_count
        self.unchanged_count += counts.unchanged_count
        assert self.rows is not None
        for impact in impacts:
            if self.sink is not None:
                self.sink(impact)
            if len(self.rows) < self.detail_limit:
                self.rows.append(impact)
        for rule_impact in rule_impacts:
            self.record_rule_precomputed(rule_impact)

    def record_persisted_precomputed(
        self,
        counts: TransformationImpactCounts,
        rule_impacts: tuple[TransformationRuleImpact, ...] = (),
    ) -> None:
        """Merge facts already persisted by a native set-based projection."""

        self.evaluated_count += counts.evaluated_count
        self.changed_count += counts.changed_count
        self.fallback_count += counts.fallback_count
        self.null_count += counts.null_count
        self.invalid_count += counts.invalid_count
        self.provided_count += counts.provided_count
        self.unchanged_count += counts.unchanged_count
        for rule_impact in rule_impacts:
            self.record_rule_precomputed(rule_impact)

    def report(self) -> TransformationImpactReport:
        assert self.rule_impacts is not None
        return TransformationImpactReport(
            mapping_content_hash=self.mapping_content_hash,
            evaluated_count=self.evaluated_count,
            changed_count=self.changed_count,
            fallback_count=self.fallback_count,
            null_count=self.null_count,
            invalid_count=self.invalid_count,
            provided_count=self.provided_count,
            unchanged_count=self.unchanged_count,
            rows=tuple(self.rows or ()),
            rule_impacts=tuple(
                self.rule_impacts[key] for key in sorted(self.rule_impacts)
            ),
            detail_limit=self.detail_limit,
        )


def _display_value(value: object) -> str:
    # Scalar mappings overwhelmingly compare primitive source and prepared
    # values.  Keep their established display semantics without first building
    # recursive portable dictionaries for every unchanged field.
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple | list):
        return " / ".join(_display_value(item) for item in value)
    portable = portable_value(value)
    if isinstance(portable, Mapping) and "value" in portable:
        return str(portable["value"])
    if isinstance(portable, list):
        return " / ".join(_display_value(item) for item in portable)
    if isinstance(portable, Mapping):
        return json.dumps(portable, ensure_ascii=False, separators=(",", ":"))
    return str(portable) if portable is not None else "—"


def _display_values_equal(left: object, right: object) -> bool:
    """Compare values using the existing normalization-review semantics."""

    if left is right:
        return True
    return _display_value(left) == _display_value(right)
