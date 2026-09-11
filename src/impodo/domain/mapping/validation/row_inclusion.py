"""Validate dataset row-inclusion rules against current source columns."""

from __future__ import annotations

from typing import Mapping

from ..contracts import DatasetMapping
from ..source_conditions import source_condition_configuration_problems
from .common import _check_column, _issue
from .context import SourceColumnView
from .evidence import MappingValidationIssue


def _validate_row_inclusion(
    dataset: DatasetMapping,
    path: str,
    columns: Mapping[str, SourceColumnView],
    issues: list[MappingValidationIssue],
) -> None:
    """Require every inclusion condition to be current and type-safe."""

    for index, condition in enumerate(dataset.row_inclusion.conditions):
        condition_path = f"{path}/row_inclusion/conditions/{index}"
        _check_column(
            dataset,
            condition.source_column_key,
            condition_path,
            columns,
            issues,
        )
        for problem in source_condition_configuration_problems(
            operator=condition.operator,
            comparison_value=condition.comparison_value,
            value_type=condition.value_type,
        ):
            issues.append(
                _issue(
                    (
                        "MAPPING_ROW_INCLUSION_VALUE_INVALID"
                        if problem.kind == "value"
                        else "MAPPING_ROW_INCLUSION_OPERATOR_INVALID"
                    ),
                    condition_path,
                    problem.message,
                    problem.remediation,
                    dataset=dataset,
                    source_column=condition.source_column_key,
                )
            )
