"""Bounded Stage-G effect construction for clean direct-table runs."""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from typing import Iterable, Iterator, Mapping

from ..domain.mapping.contracts import DatasetMapping
from ..domain.resolution import EffectiveDataset
from ..domain.staging.preparation_session import StoredCanonicalStagingRun
from ..domain.staging.transformation_impact import TransformationImpactRow
from ..normalization import (
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


class _BoundedNormalizationEffects:
    """Construct durable effects from impacts and compact canonical row IDs."""

    def __init__(
        self,
        *,
        project: MigrationProject,
        mapping_hash: str,
        mappings: Mapping[str, DatasetMapping],
        eligible_row_ids: AbstractSet[str],
    ) -> None:
        self._project = project
        self._mapping_hash = mapping_hash
        self._mappings = mappings
        self._eligible_row_ids = eligible_row_ids
        self._restricted = project.data_classification is DataClassification.RESTRICTED
        self._identity_fields = {
            dataset: {
                field
                for component in (*mapping.target_identity, *mapping.target_scope)
                for field in component.target_fields
            }
            | {item.target_field for item in mapping.relationships}
            for dataset, mapping in mappings.items()
        }

    def _effect(
        self,
        impact: TransformationImpactRow,
        row_id: str,
        *,
        eligible: bool | None = None,
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
                eligible
                if eligible is not None
                else (
                    self._eligible_row_ids.contains_canonical(row_id)
                    if callable(
                        getattr(
                            self._eligible_row_ids,
                            "contains_canonical",
                            None,
                        )
                    )
                    else row_id in self._eligible_row_ids
                )
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


class _DurableNormalizationEffects(Iterable[NormalizationEffect]):
    """Expose the construct-once session ledger as logical Stage-G effects."""

    def __init__(
        self,
        factory: _BoundedNormalizationEffects,
        impact_rows: object,
    ) -> None:
        self._factory = factory
        self._impact_rows = impact_rows

    def prepare(self) -> dict[str, object]:
        preparer = getattr(self._impact_rows, "prepare_normalization_facts")
        return preparer(
            effect_builder=self._build_effects,
            finding_builder=self._build_findings,
        )

    @property
    def prepared_run_id(self) -> str:
        return str(getattr(self._impact_rows, "normalization_run_id"))

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
                    "owner_label": (
                        issue.owner_label
                        or self._factory._project.data_manager
                        or "Data manager"
                    ),
                },
            )

    def __iter__(self) -> Iterator[NormalizationEffect]:
        yield from getattr(
            self._impact_rows,
            "iter_normalization_effects",
        )()

    def copy_to_run(self, connection, run_id: str) -> int:
        return int(
            getattr(self._impact_rows, "copy_normalization_effects")(
                connection,
                run_id,
            )
        )

    def iter_encoded_batches(self, connection, batch_size: int):
        yield from getattr(
            self._impact_rows,
            "iter_normalization_effect_json_batches",
        )(connection, batch_size)


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
    """Aggregate groups while keeping individual effects durably reusable."""

    durable_methods = (
        "prepare_normalization_facts",
        "copy_normalization_effects",
        "iter_normalization_effect_json_batches",
        "iter_normalization_effects",
    )
    if (
        effective is not None
        or quality.blocked_count
        or not all(
            callable(getattr(impact_rows, name, None)) for name in durable_methods
        )
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
        project=project,
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
    effect_count = int(summary["effect_count"])
    changed_record_count = int(summary["changed_record_count"])
    examples = summary["examples"]
    if not isinstance(examples, dict):
        raise BoundedNormalizationUnsupported
    groups = []
    for row in summary["effect_groups"]:
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
                eligible_count=int(row[9]),
                set_aside_count=int(row[10]),
                examples=tuple(
                    NormalizationExample(source_row, before, after)
                    for source_row, before, after in examples.get(
                        group_id,
                        (),
                    )
                ),
                **metadata,
            )
        )
    for row in summary["finding_groups"]:
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
                eligible_count=int(row[9]),
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
        changed_record_count=changed_record_count,
        effective_dataset_hash=staging_content_hash,
    )
