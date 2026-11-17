"""Check independent scalar uniqueness against prepared and captured Odoo data.

The planner supplies bounded, complete value lookups in the selected access
context. Updates may retain their own value; another existing record always
blocks, including when an incoming update plans to free that value later.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace

from impodo.domain.compiler.contracts import CompiledMigrationPlan
from impodo.domain.data_version.catalog import TargetCatalog
from impodo.domain.preparation.canonical import parse_value
from impodo.domain.preparation.field_uniqueness import (
    duplicate_field_message,
    unique_value_key,
)
from impodo.domain.recipe.profile import DatasetSpec, NormalizationSpec
from impodo.domain.shared.models import (
    Issue,
    PreparedRecord,
    TargetRecord,
    canonical_json_text,
    portable_value,
)

type UniqueTargetIndex = dict[tuple[str, str], dict[str, tuple[TargetRecord, ...]]]


def unique_target_index(
    plan: CompiledMigrationPlan, catalog: TargetCatalog
) -> UniqueTargetIndex:
    """Index each required Odoo field once, with canonical typed equality."""

    result: UniqueTargetIndex = {}
    for dataset in plan.datasets:
        for field, spec in dataset.fields.items():
            pair = (dataset.target.model, field)
            if not spec.must_be_unique or pair in result:
                continue
            buckets: dict[str, list[TargetRecord]] = defaultdict(list)
            for target in catalog.records(dataset.target.model):
                if field not in target.values:
                    raise ValueError(
                        f"Odoo uniqueness evidence for {dataset.target.model}.{field} is incomplete. Compare with Odoo again."
                    )
                raw = target.values.get(field)
                if raw is False and spec.type != "boolean":
                    raw = None
                value = parse_value(
                    raw, spec.type, NormalizationSpec(empty_as_null=True)
                )
                key = unique_value_key(value)
                if key is not None:
                    buckets[key].append(target)
            result[pair] = {key: tuple(group) for key, group in buckets.items()}
    return result


def incoming_unique_issues(
    plan: CompiledMigrationPlan,
    records: tuple[PreparedRecord, ...],
) -> tuple[PreparedRecord, ...]:
    """Repeat source uniqueness at preflight, including profile and Recipe runs."""

    fields = {
        (dataset.target.model, field)
        for dataset in plan.datasets
        for field, spec in dataset.fields.items()
        if spec.must_be_unique
    }
    if not fields:
        return records
    counts: Counter[tuple[str, str, str]] = Counter()
    for record in records:
        for model, field in fields:
            if model != record.target_model:
                continue
            key = unique_value_key(record.scalar_values.get(field))
            if key is not None:
                counts[(model, field, key)] += 1
    result = []
    for record in records:
        issues = []
        for model, field in sorted(fields):
            if model != record.target_model:
                continue
            key = unique_value_key(record.scalar_values.get(field))
            if key is None:
                continue
            count = counts[(model, field, key)]
            if count > 1:
                issues.append(
                    Issue(
                        code="SOURCE_FIELD_NOT_UNIQUE",
                        message=duplicate_field_message(field, count),
                        dataset=record.dataset,
                        row=record.source_row,
                        field=field,
                    )
                )
        result.append(
            replace(record, issues=(*record.issues, *issues)) if issues else record
        )
    return tuple(result)


def target_unique_issues(
    dataset: DatasetSpec,
    record: PreparedRecord,
    own_matches: tuple[TargetRecord, ...],
    index: UniqueTargetIndex,
) -> tuple[Issue, ...]:
    """Identify conflicts by business labels without exporting numeric Odoo IDs."""

    if not index:
        return ()
    issues = []
    own_id = own_matches[0].odoo_id if len(own_matches) == 1 else None
    for field in sorted(dataset.fields):
        values = index.get((dataset.target.model, field))
        if values is None:
            continue
        key = unique_value_key(record.scalar_values.get(field))
        if key is None:
            continue
        conflicts = tuple(
            target for target in values.get(key, ()) if target.odoo_id != own_id
        )
        if not conflicts:
            continue
        label_fields = tuple(
            field
            for component in dataset.target_identity.components
            if component.resolve is None
            for field in component.target_fields
        )
        labels = (
            [
                canonical_json_text(
                    portable_value(
                        tuple(target.values.get(name) for name in label_fields)
                    )
                )[:250]
                for target in conflicts[:5]
            ]
            if label_fields
            else []
        )
        value = canonical_json_text(portable_value(record.scalar_values[field]))[:250]
        detail = f" Matching Odoo identifiers: {', '.join(labels)}." if labels else ""
        issues.append(
            Issue(
                code="TARGET_FIELD_NOT_UNIQUE",
                message=(
                    f"{field} value {value} is already used by {len(conflicts)} other Odoo records."
                    f"{detail} Correct the value or the existing record, then compare again."
                ),
                dataset=record.dataset,
                row=record.source_row,
                field=field,
            )
        )
    return tuple(issues)
