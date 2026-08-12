"""Bounded Stage-G effect construction for clean direct-table runs."""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping

from ..domain.mapping.contracts import DatasetMapping
from ..domain.resolution import EffectiveDataset
from ..domain.staging.preparation_session import StoredCanonicalStagingRun
from ..domain.staging.transformation_impact import TransformationImpactRow
from ..normalization import (
    NORMALIZATION_EXAMPLE_LIMIT,
    NORMALIZATION_POLICY_VERSION,
    NormalizationCandidate,
    NormalizationEffect,
    NormalizationExample,
    NormalizationGroupKind,
    NormalizationOutcome,
    NormalizationReviewGroup,
    StoredNormalizationEvaluation,
    _change_language,
    _hash,
    _human_label,
    _policy_manifest,
    _protected_display,
    _review_outcome,
    canonical_eligible_dataset_hash,
)
from ..projects import DataClassification, MigrationProject
from ..quality import (
    QualityOutcomePolicy,
    StoredQualityRun,
    retention_context_hash,
)


class BoundedNormalizationUnsupported(ValueError):
    """Signal that the complete normalization evaluator is required."""


@dataclass(slots=True)
class _GroupAccumulator:
    metadata: dict[str, object]
    eligible_count: int = 0
    set_aside_count: int = 0
    examples: list[tuple[tuple[int, str, str], NormalizationExample]] | None = None

    def add(self, effect: NormalizationEffect) -> None:
        if effect.eligible:
            self.eligible_count += 1
            if self.examples is None:
                self.examples = []
            self.examples.append(
                (
                    (effect.source_row, effect.row_id, effect.effect_id),
                    NormalizationExample(
                        effect.source_row,
                        effect.before,
                        effect.after,
                    ),
                )
            )
            self.examples.sort(key=lambda item: item[0])
            del self.examples[NORMALIZATION_EXAMPLE_LIMIT:]
        else:
            self.set_aside_count += 1


class _BoundedNormalizationEffects(Iterable[NormalizationEffect]):
    """Replay effects from durable impacts and compact canonical row IDs."""

    def __init__(
        self,
        *,
        project: MigrationProject,
        mapping_hash: str,
        mappings: Mapping[str, DatasetMapping],
        impact_rows: object,
        eligible_row_ids: AbstractSet[str],
    ) -> None:
        self._project = project
        self._mapping_hash = mapping_hash
        self._mappings = mappings
        self._impact_rows = impact_rows
        self._eligible_row_ids = eligible_row_ids
        self._restricted = (
            project.data_classification is DataClassification.RESTRICTED
        )
        self._identity_fields = {
            dataset: {
                field
                for component in (*mapping.target_identity, *mapping.target_scope)
                for field in component.target_fields
            }
            | {item.target_field for item in mapping.relationships}
            for dataset, mapping in mappings.items()
        }

    def __iter__(self) -> Iterator[NormalizationEffect]:
        for effect, _metadata in self.iter_with_metadata():
            yield effect

    def iter_with_metadata(self, *, connection=None):
        bound_reader = getattr(self._impact_rows, "iter_bound_rows", None)
        if not callable(bound_reader):
            raise BoundedNormalizationUnsupported
        seen_effect_ids: set[str] = set()
        for impact, row_id in bound_reader(connection=connection):
            built = self._effect(impact, row_id)
            if built is None:
                continue
            effect, metadata = built
            if effect.effect_id in seen_effect_ids:
                continue
            seen_effect_ids.add(effect.effect_id)
            yield effect, metadata

    def iter_batches(self, connection, batch_size: int):
        batch: list[NormalizationEffect] = []
        for effect, _metadata in self.iter_with_metadata(connection=connection):
            batch.append(effect)
            if len(batch) >= batch_size:
                yield tuple(batch)
                batch.clear()
        if batch:
            yield tuple(batch)

    def _effect(
        self,
        impact: TransformationImpactRow,
        row_id: str,
    ) -> tuple[NormalizationEffect, dict[str, object]] | None:
        if impact.outcome == "invalid":
            return None
        mapping = self._mappings.get(impact.dataset)
        if mapping is None:
            raise BoundedNormalizationUnsupported
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
        outcome = _review_outcome(
            candidate,
            identity_impact=(
                candidate.target_field
                in self._identity_fields.get(candidate.dataset, set())
            ),
        )
        rule_id = _hash(
            {
                "mapping_hash": self._mapping_hash,
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
        before = _protected_display(
            candidate.raw_display,
            restricted=self._restricted,
        )
        after = _protected_display(
            candidate.proposed_display,
            restricted=self._restricted,
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
                self._eligible_row_ids.contains_canonical(row_id)
                if callable(
                    getattr(
                        self._eligible_row_ids,
                        "contains_canonical",
                        None,
                    )
                )
                else row_id in self._eligible_row_ids
            ),
        )
        name, explanation = _change_language(candidate.rules, outcome)
        return effect, {
            "rule_id": rule_id,
            "kind": NormalizationGroupKind.CHANGE,
            "outcome": outcome,
            "dataset": candidate.dataset,
            "target_field": candidate.target_field,
            "name": name,
            "explanation": explanation,
            "owner_label": self._project.data_manager or "Data manager",
        }


def build_bounded_normalization_evaluation(
    *,
    project: MigrationProject,
    staging: StoredCanonicalStagingRun,
    quality: StoredQualityRun,
    mappings: Mapping[str, DatasetMapping],
    impact_rows: object,
    staging_content_hash: str,
    quality_content_hash: str,
    effective: EffectiveDataset | None,
) -> StoredNormalizationEvaluation:
    """Aggregate groups while leaving individual effects replayable on disk."""

    if (
        effective is not None
        or quality.blocked_count
        or not callable(getattr(impact_rows, "iter_bound_rows", None))
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
    effects = _BoundedNormalizationEffects(
        project=project,
        mapping_hash=staging.mapping_hash,
        mappings=mappings,
        impact_rows=impact_rows,
        eligible_row_ids=quality.eligible_row_ids,
    )
    accumulators: dict[str, _GroupAccumulator] = {}
    changed_row_ids: set[str] = set()
    effect_count = 0
    for effect, metadata in effects.iter_with_metadata():
        accumulator = accumulators.get(effect.group_id)
        if accumulator is None:
            accumulator = _GroupAccumulator(metadata=metadata)
            accumulators[effect.group_id] = accumulator
        elif accumulator.metadata != metadata:
            raise BoundedNormalizationUnsupported
        accumulator.add(effect)
        effect_count += 1
        if effect.eligible:
            changed_row_ids.add(effect.row_id)

    groups: list[NormalizationReviewGroup] = [
        NormalizationReviewGroup(
            group_id=group_id,
            dataset_label=_human_label(str(accumulator.metadata["dataset"])),
            field_label=_human_label(
                str(accumulator.metadata["target_field"])
            ),
            eligible_count=accumulator.eligible_count,
            set_aside_count=accumulator.set_aside_count,
            examples=tuple(
                item[1] for item in (accumulator.examples or ())
            ),
            **accumulator.metadata,
        )
        for group_id, accumulator in sorted(accumulators.items())
    ]
    warning_rows: dict[str, set[str]] = {}
    warning_metadata: dict[str, dict[str, object]] = {}
    for issue in quality.issues:
        if (
            issue.policy is not QualityOutcomePolicy.WARNING
            or issue.row_id is None
            or issue.row_id not in quality.eligible_row_ids
        ):
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
        warning_rows.setdefault(group_id, set()).add(issue.row_id)
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
                "owner_label": (
                    issue.owner_label or project.data_manager or "Data manager"
                ),
            },
        )
    for group_id, metadata in sorted(warning_metadata.items()):
        groups.append(
            NormalizationReviewGroup(
                group_id=group_id,
                dataset_label=_human_label(str(metadata["dataset"])),
                field_label=_human_label(str(metadata["target_field"])),
                eligible_count=len(warning_rows[group_id]),
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
        project_id=project.project_id,
        staging_content_hash=staging_content_hash,
        quality_content_hash=quality_content_hash,
        mapping_hash=staging.mapping_hash,
        schema_hash=staging.schema_hash,
        policy_hash=policy_hash,
        retention_context_hash=retention_context_hash(project),
        eligible_dataset_hash=eligible_dataset_hash,
        effects=effects,
        groups=tuple(sorted(groups, key=lambda item: item.group_id)),
        effect_count=effect_count,
        changed_record_count=len(changed_row_ids),
        effective_dataset_hash=staging_content_hash,
    )
