"""Bounded normalization-effect construction for clean direct-table runs."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from typing import Protocol, runtime_checkable

from impodo.domain.mapping.contracts import DatasetMapping
from impodo.domain.preparation.normalization import (
    NORMALIZATION_POLICY_VERSION,
    NormalizationCandidate,
    NormalizationEffect,
    NormalizationExample,
    NormalizationGroupKind,
    NormalizationOutcome,
    NormalizationReviewGroup,
    StoredNormalizationEvaluation,
    _hash,
    _human_label,
    _policy_manifest,
    _protected_display,
    canonical_eligible_dataset_hash,
    compile_normalization_review_policy,
    normalization_change_language,
)
from impodo.domain.preparation.quality import (
    QualityOutcomePolicy,
    StoredQualityRun,
    retention_context_hash,
)
from impodo.domain.resolution import EffectiveDataset
from impodo.domain.staging.preparation_session import StoredCanonicalStagingRun
from impodo.domain.staging.transformation_impact import TransformationImpactRow
from impodo.domain.workspace.workbench import DataClassification, WorkspaceState


class BoundedNormalizationUnsupported(ValueError):
    """Signal that the complete normalization evaluator is required."""


@runtime_checkable
class _NormalizationImpactRows(Protocol):
    """Session-row capabilities required by the bounded normalizer."""

    @property
    def normalization_run_id(self) -> str: ...

    def prepare_normalization_facts(
        self,
        **builders: object,
    ) -> dict[str, object]: ...

    def iter_normalization_effects(self) -> Iterator[NormalizationEffect]: ...

    def copy_normalization_effects(
        self,
        connection: object,
        run_id: str,
    ) -> int: ...

    def iter_normalization_effect_json_batches(
        self,
        connection: object,
        batch_size: int,
    ) -> Iterator[object]: ...


def _summary_int(value: object, label: str) -> int:
    """Decode one bounded summary count without arbitrary coercion."""

    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise BoundedNormalizationUnsupported(f"{label} is invalid")
    try:
        return int(value)
    except ValueError as error:
        raise BoundedNormalizationUnsupported(f"{label} is invalid") from error


def _summary_rows(
    summary: Mapping[str, object],
    key: str,
) -> tuple[tuple[object, ...], ...]:
    """Validate one bounded normalization group table."""

    raw_rows = summary.get(key)
    if not isinstance(raw_rows, (list, tuple)):
        raise BoundedNormalizationUnsupported(f"{key} is invalid")
    rows: list[tuple[object, ...]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, (list, tuple)) or len(raw_row) < 11:
            raise BoundedNormalizationUnsupported(f"{key} row is invalid")
        rows.append(tuple(raw_row))
    return tuple(rows)


def _summary_examples(
    value: object,
) -> dict[str, tuple[NormalizationExample, ...]]:
    """Validate the bounded example projection for every effect group."""

    if not isinstance(value, Mapping):
        raise BoundedNormalizationUnsupported("normalization examples are invalid")
    examples: dict[str, tuple[NormalizationExample, ...]] = {}
    for group_id, raw_examples in value.items():
        if not isinstance(group_id, str) or not isinstance(
            raw_examples,
            (list, tuple),
        ):
            raise BoundedNormalizationUnsupported("normalization examples are invalid")
        group_examples: list[NormalizationExample] = []
        for raw_example in raw_examples:
            if not isinstance(raw_example, (list, tuple)) or len(raw_example) != 3:
                raise BoundedNormalizationUnsupported(
                    "normalization example is invalid"
                )
            source_row, before, after = raw_example
            if not isinstance(before, str) or not isinstance(after, str):
                raise BoundedNormalizationUnsupported(
                    "normalization example values are invalid"
                )
            group_examples.append(
                NormalizationExample(
                    _summary_int(source_row, "normalization example row"),
                    before,
                    after,
                )
            )
        examples[group_id] = tuple(group_examples)
    return examples


class _BoundedNormalizationEffects:
    """Construct durable effects from impacts and compact canonical row IDs."""

    def __init__(
        self,
        *,
        workspace_state: WorkspaceState,
        mapping_hash: str,
        mappings: Mapping[str, DatasetMapping],
        eligible_row_ids: AbstractSet[str],
    ) -> None:
        self._workspace_state = workspace_state
        self._mapping_hash = mapping_hash
        self._eligible_row_ids = eligible_row_ids
        self._restricted = (
            workspace_state.data_classification is DataClassification.RESTRICTED
        )
        self._review_policy = compile_normalization_review_policy(mappings)

    def _effect(
        self,
        impact: TransformationImpactRow,
        row_id: str,
        *,
        eligible: bool | None = None,
    ) -> tuple[NormalizationEffect, dict[str, object]] | None:
        if impact.outcome == "invalid":
            return None
        candidate = NormalizationCandidate(
            dataset=impact.dataset,
            source_row=impact.source_row,
            source_label=impact.source_column,
            target_field=impact.target_field,
            raw_display=impact.raw_value,
            proposed_display=impact.proposed_value,
            rules=impact.rules,
            outcome=impact.outcome,
            message=impact.message,
        )
        field_policy = self._review_policy.resolve(candidate)
        outcome = field_policy.outcome
        rule_id = _hash(
            {
                "mapping_hash": self._mapping_hash,
                "dataset": candidate.dataset,
                "target_field": candidate.target_field,
                "review_policy": field_policy.to_portable_dict(),
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
        before = _protected_display(
            candidate.raw_display,
            restricted=self._restricted,
        )
        after = _protected_display(
            candidate.proposed_display,
            restricted=self._restricted,
        )
        contains_canonical = getattr(
            self._eligible_row_ids,
            "contains_canonical",
            None,
        )
        effect = NormalizationEffect(
            effect_id=_hash(
                {
                    "group_id": group_id,
                    "row_id": row_id,
                    "before": before,
                    "after": after,
                }
            ),
            group_id=group_id,
            row_id=row_id,
            dataset=candidate.dataset,
            source_row=candidate.source_row,
            target_field=candidate.target_field,
            before=before,
            after=after,
            eligible=(
                eligible
                if eligible is not None
                else (
                    bool(contains_canonical(row_id))
                    if callable(contains_canonical)
                    else row_id in self._eligible_row_ids
                )
            ),
        )
        name, explanation = normalization_change_language(field_policy)
        return effect, {
            "rule_id": rule_id,
            "kind": NormalizationGroupKind.CHANGE,
            "outcome": outcome,
            "dataset": candidate.dataset,
            "target_field": candidate.target_field,
            "name": name,
            "explanation": explanation,
            "owner_label": "Data manager",
        }


class _DurableNormalizationEffects(Iterable[NormalizationEffect]):
    """Expose the construct-once session ledger as normalization effects."""

    def __init__(
        self,
        factory: _BoundedNormalizationEffects,
        impact_rows: _NormalizationImpactRows,
    ) -> None:
        self._factory = factory
        self._impact_rows = impact_rows

    def prepare(self) -> dict[str, object]:
        """Build durable normalization facts through the session-backed index.

        Called by the normalization service's bounded route. The wrapped store
        owns indexing and persistence; this adapter only supplies factories for
        domain-shaped effects and findings.
        """
        return self._impact_rows.prepare_normalization_facts(
            effect_builder=self._build_effects,
            finding_builder=self._build_findings,
        )

    @property
    def prepared_run_id(self) -> str:
        """Expose the source run identifier required by the durable publisher."""
        return str(self._impact_rows.normalization_run_id)

    def _build_effects(self, rows):
        for impact, row_id, eligible in rows:
            built = self._factory._effect(
                impact,
                row_id,
                eligible=bool(eligible),
            )
            if built is not None:
                yield built

    def _build_findings(self, issues):
        for issue in issues:
            if issue.policy is not QualityOutcomePolicy.WARNING or issue.row_id is None:
                continue
            target_field = (
                issue.affected_fields[0] if issue.affected_fields else "review"
            )
            group_id = _hash(
                {
                    "kind": NormalizationGroupKind.FINDING.value,
                    "rule_id": issue.rule_id,
                    "dataset": issue.dataset,
                    "field": target_field,
                    "reason": issue.reason_code,
                }
            )
            yield (
                group_id,
                issue.issue_id,
                issue.row_id,
                {
                    "rule_id": issue.rule_id,
                    "kind": NormalizationGroupKind.FINDING,
                    "outcome": NormalizationOutcome.REVIEW_FINDING,
                    "dataset": issue.dataset,
                    "target_field": target_field,
                    "name": "Review this data finding",
                    "explanation": issue.message,
                    "owner_label": (issue.owner_label or "Data manager"),
                },
            )

    def __iter__(self) -> Iterator[NormalizationEffect]:
        yield from self._impact_rows.iter_normalization_effects()

    def copy_to_run(self, connection, run_id: str) -> int:
        """Copy indexed effects into the published run without materializing them."""
        return int(
            self._impact_rows.copy_normalization_effects(
                connection,
                run_id,
            )
        )

    def iter_encoded_batches(self, connection, batch_size: int):
        """Stream encoded durable effects for consumers that need batch access."""
        yield from self._impact_rows.iter_normalization_effect_json_batches(
            connection,
            batch_size,
        )


def build_bounded_normalization_evaluation(
    *,
    workspace_state: WorkspaceState,
    staging: StoredCanonicalStagingRun,
    quality: StoredQualityRun,
    mappings: Mapping[str, DatasetMapping],
    impact_rows: object,
    staging_content_hash: str,
    quality_content_hash: str,
    effective: EffectiveDataset | None,
) -> StoredNormalizationEvaluation:
    """Aggregate groups while keeping individual effects durably reusable."""

    if (
        effective is not None
        or quality.blocked_count
        or not isinstance(impact_rows, _NormalizationImpactRows)
    ):
        raise BoundedNormalizationUnsupported
    if quality.staging_content_hash != staging_content_hash:
        raise BoundedNormalizationUnsupported

    policy_hash = _hash(
        {
            "policy_version": NORMALIZATION_POLICY_VERSION,
            "mapping_hash": staging.mapping_hash,
            "rules": _policy_manifest(mappings),
        }
    )
    effect_factory = _BoundedNormalizationEffects(
        workspace_state=workspace_state,
        mapping_hash=staging.mapping_hash,
        mappings=mappings,
        eligible_row_ids=quality.eligible_row_ids,
    )
    durable_effects = _DurableNormalizationEffects(
        effect_factory,
        impact_rows,
    )
    effects: Iterable[NormalizationEffect] = durable_effects
    summary = durable_effects.prepare()
    effect_count = _summary_int(summary["effect_count"], "effect count")
    changed_record_count = _summary_int(
        summary["changed_record_count"],
        "changed record count",
    )
    examples = _summary_examples(summary["examples"])
    groups = []
    for row in _summary_rows(summary, "effect_groups"):
        group_id = str(row[0])
        metadata = {
            "rule_id": str(row[1]),
            "kind": NormalizationGroupKind(str(row[2])),
            "outcome": NormalizationOutcome(str(row[3])),
            "dataset": str(row[4]),
            "target_field": str(row[5]),
            "name": str(row[6]),
            "explanation": str(row[7]),
            "owner_label": str(row[8]),
        }
        groups.append(
            NormalizationReviewGroup(
                group_id=group_id,
                dataset_label=_human_label(str(row[4])),
                field_label=_human_label(str(row[5])),
                eligible_count=_summary_int(row[9], "eligible count"),
                set_aside_count=_summary_int(row[10], "set-aside count"),
                examples=examples.get(group_id, ()),
                **metadata,
            )
        )
    for row in _summary_rows(summary, "finding_groups"):
        metadata = {
            "rule_id": str(row[1]),
            "kind": NormalizationGroupKind(str(row[2])),
            "outcome": NormalizationOutcome(str(row[3])),
            "dataset": str(row[4]),
            "target_field": str(row[5]),
            "name": str(row[6]),
            "explanation": str(row[7]),
            "owner_label": str(row[8]),
        }
        groups.append(
            NormalizationReviewGroup(
                group_id=str(row[0]),
                dataset_label=_human_label(str(row[4])),
                field_label=_human_label(str(row[5])),
                eligible_count=_summary_int(row[9], "finding count"),
                set_aside_count=0,
                examples=(),
                **metadata,
            )
        )
    eligible_dataset_hash = canonical_eligible_dataset_hash(
        staging,
        quality,
        staging_content_hash=staging_content_hash,
        quality_content_hash=quality_content_hash,
    )
    return StoredNormalizationEvaluation(
        workspace_id=workspace_state.workspace_id,
        staging_content_hash=staging_content_hash,
        quality_content_hash=quality_content_hash,
        mapping_hash=staging.mapping_hash,
        schema_hash=staging.schema_hash,
        policy_hash=policy_hash,
        retention_context_hash=retention_context_hash(workspace_state),
        eligible_dataset_hash=eligible_dataset_hash,
        effects=effects,
        groups=tuple(sorted(groups, key=lambda item: item.group_id)),
        effect_count=effect_count,
        changed_record_count=changed_record_count,
        effective_dataset_hash=staging_content_hash,
    )
