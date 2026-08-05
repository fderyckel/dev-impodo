"""Shared primitives for mapping validation rules."""

from __future__ import annotations

from typing import Mapping

from ....metadata import TYPE_COMPATIBILITY
from ..contracts import DatasetMapping
from .context import SourceColumnView
from .evidence import MappingValidationIssue


_RELATION_TYPES = frozenset({"many2one", "many2many", "one2many"})
_VALUE_TYPES = frozenset(TYPE_COMPATIBILITY)
_NULL_POLICIES = frozenset(
    {"distinct", "equivalent", "ignore_source_null"}
)


def _check_column(
    dataset: DatasetMapping,
    column: str,
    path: str,
    columns: Mapping[str, SourceColumnView],
    issues: list[MappingValidationIssue],
) -> None:
    if column not in columns:
        issues.append(
            _issue(
                "MAPPING_SOURCE_COLUMN_UNKNOWN",
                path,
                "The mapping references an unknown source column.",
                "Choose a column from the current frozen dataset.",
                dataset=dataset,
                source_column=column,
            )
        )


def _target_unknown(
    dataset: DatasetMapping,
    path: str,
    target_field: str,
) -> MappingValidationIssue:
    return _issue(
        "MAPPING_TARGET_FIELD_UNKNOWN",
        path,
        f"Target field {dataset.target_model}.{target_field} is unavailable.",
        "Choose a field from the captured schema.",
        dataset=dataset,
        target_field=target_field,
    )


def _issue(
    code: str,
    path: str,
    message: str,
    remediation: str,
    *,
    severity: str = "error",
    dataset: DatasetMapping | None = None,
    source_column: str | None = None,
    target_field: str | None = None,
) -> MappingValidationIssue:
    return MappingValidationIssue(
        code=code,
        severity=severity,
        path=path,
        message=message,
        remediation=remediation,
        dataset_id=dataset.dataset_id if dataset else None,
        source_column_key=source_column,
        target_model=dataset.target_model if dataset else None,
        target_field=target_field,
    )
