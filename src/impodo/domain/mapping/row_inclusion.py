"""Deterministic dataset row-inclusion evaluation."""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import (
    RowInclusionJoin,
    RowInclusionMode,
    RowInclusionPolicy,
)
from .source_conditions import SourceConditionValueError, source_condition_matches


def row_is_included(
    policy: RowInclusionPolicy,
    source_values_by_key: Mapping[str, Any],
) -> bool:
    """Return whether one row belongs to the mapped dataset population."""

    if policy.mode is RowInclusionMode.ALL_ROWS:
        return True
    matches: list[bool] = []
    for condition in policy.conditions:
        try:
            matches.append(
                source_condition_matches(
                    raw_value=source_values_by_key.get(
                        condition.source_column_key
                    ),
                    operator=condition.operator,
                    comparison_value=condition.comparison_value,
                    value_type=condition.value_type,
                )
            )
        except SourceConditionValueError as error:
            raise SourceConditionValueError(
                str(error),
                source_column_key=condition.source_column_key,
            ) from error
    if policy.join is RowInclusionJoin.ALL:
        return all(matches)
    return any(matches)
