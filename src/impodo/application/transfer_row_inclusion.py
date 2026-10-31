"""Evaluate transfer row rules locally before destination reads and loading."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from impodo.domain.mapping.contracts import RowInclusionMode, RowInclusionPolicy
from impodo.domain.mapping.row_inclusion import row_is_included
from impodo.domain.mapping.source_conditions import (
    SourceConditionValueError,
    source_condition_configuration_problems,
)
from impodo.domain.preparation.source import SourceRow
from impodo.domain.workspace.contracts import SourceDataset
from impodo.domain.workspace.destination_matching import DestinationModelMatch
from impodo.domain.workspace.errors import WorkspaceError


class TransferRuleSourceValues(Protocol):
    """Project one bounded set of frozen source columns in physical row order."""

    def source_key_tuples(
        self, workspace_id: str, dataset_id: str, source_column_keys: tuple[str, ...]
    ) -> Sequence[Sequence[Any]]: ...


@dataclass(frozen=True)
class _FrozenRuleValues:
    dataset: SourceDataset
    rows: tuple[SourceRow, ...]

    def source_key_tuples(
        self, workspace_id: str, dataset_id: str, source_column_keys: tuple[str, ...]
    ) -> Sequence[Sequence[Any]]:
        names = {item.stable_key: item.source_name for item in self.dataset.columns}
        return tuple(
            tuple(row.values.get(names[key]) for key in source_column_keys)
            for row in self.rows
        )


def verify_transfer_row_inclusion(
    dataset: SourceDataset, match: DestinationModelMatch, rows: tuple[SourceRow, ...]
) -> None:
    """Recheck frozen rule decisions before creating an execution snapshot."""

    excluded, blocked = evaluate_transfer_row_inclusion(
        _FrozenRuleValues(dataset, rows), "", dataset, match.row_inclusion
    )
    if (
        excluded != match.rule_excluded_source_row_numbers
        or blocked != match.rule_blocked_source_row_numbers
        or not match.ready
    ):
        raise WorkspaceError(
            "The checked rows to use changed. Check destination matching again."
        )


def evaluate_transfer_row_inclusion(
    source_values: TransferRuleSourceValues,
    workspace_id: str,
    dataset: SourceDataset,
    policy: RowInclusionPolicy,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return excluded and cannot-evaluate ordinals from the shared evaluator.

    Projection occurs once before the row loop. No repository or Odoo calls
    occur while evaluating source values. The caller retains every ordinal
    and must block unsafe or empty populations before confirming a transfer.
    """

    if policy.mode is RowInclusionMode.ALL_ROWS:
        return (), ()
    columns = {item.stable_key for item in dataset.columns}
    for condition in policy.conditions:
        if condition.source_column_key not in columns:
            raise WorkspaceError(
                f"Choose a current rows-to-use column for {dataset.name}"
            )
        problems = source_condition_configuration_problems(
            operator=condition.operator,
            comparison_value=condition.comparison_value,
            value_type=condition.value_type,
        )
        if problems:
            raise WorkspaceError(f"{dataset.name}: {problems[0].message}")
    keys = tuple(dict.fromkeys(item.source_column_key for item in policy.conditions))
    rows = source_values.source_key_tuples(workspace_id, dataset.dataset_id, keys)
    if len(rows) != dataset.row_count or any(len(row) != len(keys) for row in rows):
        raise WorkspaceError(f"The frozen rows for {dataset.name} are inconsistent")
    excluded: list[int] = []
    blocked: list[int] = []
    for number, row in enumerate(rows, start=1):
        try:
            if not row_is_included(policy, dict(zip(keys, row, strict=True))):
                excluded.append(number)
        except SourceConditionValueError:
            blocked.append(number)
    return tuple(excluded), tuple(blocked)
