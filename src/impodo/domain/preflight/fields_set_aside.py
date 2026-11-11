"""Project saved field omissions for the portable Final review workbook."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from ..compiler.contracts import CompiledMigrationPlan
from ..preparation.staging_contracts import CanonicalRow


class FieldSetAsideReason(StrEnum):
    """Business labels for distinct saved omission reasons."""

    INVALID = "Invalid format or value"
    MISSING = "Required value missing"
    UNRECOGNIZED = "Value not recognized"
    AMBIGUOUS = "Multiple matches"
    EXCLUDED = "Intentionally excluded"
    RULE = "Skipped by rule"


@dataclass(frozen=True, slots=True)
class FieldSetAside:
    """A saved field finding with logical source and target coordinates."""

    reason: FieldSetAsideReason
    dataset: str
    source_row: int | None
    source_keys: tuple[str, ...]
    target_field: str
    detail: str
    source_value: str | None = None


@dataclass(frozen=True, slots=True)
class FieldSetAsideEvidence:
    """Field findings and rules derived from the verified preparation input."""

    findings: tuple[FieldSetAside, ...]
    source_labels: Mapping[tuple[str, str], str]
    source_keys: Mapping[tuple[str, str], tuple[str, ...]]
    null_omissions: Mapping[str, tuple[str, ...]]
    upsert_datasets: frozenset[str]
    target_models: Mapping[str, str]


_PREPARATION_REASONS = {
    "SOURCE_TYPE_INVALID": FieldSetAsideReason.INVALID,
    "SOURCE_IDENTITY_INVALID": FieldSetAsideReason.INVALID,
    "SOURCE_FORMULA_INVALID": FieldSetAsideReason.INVALID,
    "SOURCE_SELECTION_RULE_SOURCE_INVALID": FieldSetAsideReason.INVALID,
    "SOURCE_RULE_OUTPUT_TOO_LONG": FieldSetAsideReason.INVALID,
    "SOURCE_FIELD_MISSING": FieldSetAsideReason.MISSING,
    "SOURCE_REQUIRED_VALUE_MISSING": FieldSetAsideReason.MISSING,
    "SOURCE_CONCATENATION_PART_BLANK": FieldSetAsideReason.MISSING,
    "REFERENCE_KEY_BLANK": FieldSetAsideReason.MISSING,
    "REFERENCE_KEY_UNKNOWN": FieldSetAsideReason.UNRECOGNIZED,
    "SOURCE_SELECTION_RULE_UNRESOLVED": FieldSetAsideReason.UNRECOGNIZED,
    "DERIVED_REFERENCE_MISSING": FieldSetAsideReason.UNRECOGNIZED,
    "DERIVED_REFERENCE_QUARANTINED": FieldSetAsideReason.UNRECOGNIZED,
}


def preparation_field_reason(code: str) -> FieldSetAsideReason | None:
    """Classify a saved preparation issue that prevented a usable field value."""

    return _PREPARATION_REASONS.get(code)


def field_set_aside_evidence(
    plan: CompiledMigrationPlan,
    rows: Iterable[CanonicalRow],
    source_labels: Mapping[tuple[str, str], str],
    source_keys: Mapping[tuple[str, str], tuple[str, ...]],
) -> FieldSetAsideEvidence:
    """Read preparation findings and explicit rules without re-evaluating data."""

    findings: list[FieldSetAside] = []
    null_omissions: dict[str, tuple[str, ...]] = {}
    scalar_targets: dict[tuple[str, str], list[str]] = {}
    relation_targets: set[tuple[str, str]] = set()
    for dataset in plan.datasets:
        for field, spec in dataset.fields.items():
            scalar_targets.setdefault((dataset.name, spec.source), []).append(field)
        relation_targets.update((dataset.name, field) for field in dataset.relations)
        providers = {**dataset.fields, **dataset.relations}
        identity_fields = {
            field
            for part in (
                *dataset.target_identity.components,
                *dataset.target_identity.scope,
            )
            for field in part.target_fields
        }
        null_omissions[dataset.name] = tuple(
            field
            for field, spec in sorted(providers.items())
            if spec.null_policy == "ignore_source_null"
            and not spec.validate_only
            and spec.compare
            and field not in identity_fields
            and source_keys.get((dataset.name, field))
        )
        for field, spec in sorted(providers.items()):
            keys = source_keys.get((dataset.name, field), ())
            if spec.validate_only and keys and field not in identity_fields:
                findings.extend(
                    FieldSetAside(
                        FieldSetAsideReason.EXCLUDED,
                        dataset.name,
                        None,
                        (key,),
                        field,
                        "The saved mapping uses this column for validation only.",
                    )
                    for key in keys
                )

    for row in rows:
        for issue in row.issues:
            reason = preparation_field_reason(issue.code)
            if reason is None or not issue.field:
                continue
            coordinate = (row.dataset, issue.field)
            targets = scalar_targets.get(coordinate)
            if targets is None:
                targets = [issue.field] if coordinate in relation_targets else [""]
            for target in targets:
                keys = row.lineage.field_sources.get(target, ())
                if not keys and not target:
                    keys = (issue.field,)
                findings.append(
                    FieldSetAside(
                        reason, row.dataset, row.source_row, keys, target, issue.message
                    )
                )

    return FieldSetAsideEvidence(
        findings=tuple(findings),
        source_labels=source_labels,
        source_keys=source_keys,
        null_omissions=null_omissions,
        upsert_datasets=frozenset(
            dataset.name for dataset in plan.datasets if dataset.target.mode in {"upsert", "update"}
        ),
        target_models={dataset.name: dataset.target.model for dataset in plan.datasets},
    )
