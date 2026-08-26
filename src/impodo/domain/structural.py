"""Bounded structural preparation rules with complete multi-source lineage.

Migration stage: D-E. Layer: domain behavior. These exact join, union-all, and
grouping rules operate only on already frozen ``SourceTable`` values. They do
not accept SQL or code and never contact Odoo.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from impodo.domain.preparation.canonical import ValueParseError, parse_value
from ..domain.serialization import content_hash
from ..domain.source_binding import DerivedSourceBinding
from impodo.domain.shared.models import portable_value
from impodo.domain.recipe.profile import NormalizationSpec
from impodo.domain.preparation.source import SourceRow, SourceTable
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)


STRUCTURAL_CONTRACT_VERSION = 1
MAX_STRUCTURAL_COLUMNS = 200
MAX_STRUCTURAL_BRANCHES = 10
MAX_STRUCTURAL_KEYS = 5
_VALUE_TYPES = frozenset({"string", "integer", "decimal", "boolean", "date", "datetime"})


class StructuralError(ValueError):
    """Raised when a structural rule cannot reconcile without data loss."""


class JoinKind(StrEnum):
    LEFT = "LEFT"
    INNER = "INNER"


class AggregateOperation(StrEnum):
    COUNT = "COUNT"
    SUM = "SUM"


@dataclass(frozen=True, slots=True)
class StructuralOutputColumn:
    column_key: str
    source_name: str
    candidate_type: str

    def __post_init__(self) -> None:
        _column_key(self.column_key)
        _text(self.source_name, "structural column label", 200)
        if self.candidate_type not in _VALUE_TYPES:
            raise ValueError("Structural output type is unsupported")


@dataclass(frozen=True, slots=True)
class StructuralProjection:
    output_column_key: str
    source_dataset_id: str
    source_column_key: str

    def __post_init__(self) -> None:
        _column_key(self.output_column_key)
        _text(self.source_dataset_id, "structural source dataset", 200)
        _column_key(self.source_column_key)


@dataclass(frozen=True, slots=True)
class JoinKey:
    left_column_key: str
    right_column_key: str
    value_type: str = "string"

    def __post_init__(self) -> None:
        _column_key(self.left_column_key)
        _column_key(self.right_column_key)
        if self.value_type not in _VALUE_TYPES:
            raise ValueError("Join-key type is unsupported")


@dataclass(frozen=True, slots=True)
class ExactJoinRule:
    rule_id: str
    output_dataset_name: str
    left_dataset_id: str
    right_dataset_id: str
    keys: tuple[JoinKey, ...]
    output_columns: tuple[StructuralOutputColumn, ...]
    projections: tuple[StructuralProjection, ...]
    kind: JoinKind = JoinKind.LEFT
    require_all_right_rows: bool = True

    def __post_init__(self) -> None:
        _uuid(self.rule_id, "join rule ID")
        _dataset_name(self.output_dataset_name)
        _text(self.left_dataset_id, "left join dataset", 200)
        _text(self.right_dataset_id, "right join dataset", 200)
        if self.left_dataset_id == self.right_dataset_id:
            raise ValueError("Join inputs must be different datasets")
        if not 1 <= len(self.keys) <= MAX_STRUCTURAL_KEYS:
            raise ValueError("Joins require one to five exact keys")
        object.__setattr__(self, "kind", JoinKind(self.kind))
        _validate_output(self.output_columns, self.projections)
        allowed = {self.left_dataset_id, self.right_dataset_id}
        if any(item.source_dataset_id not in allowed for item in self.projections):
            raise ValueError("Join projection references another dataset")

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "output_dataset_name": self.output_dataset_name,
            "left_dataset_id": self.left_dataset_id,
            "right_dataset_id": self.right_dataset_id,
            "keys": [asdict(item) for item in self.keys],
            "output_columns": [asdict(item) for item in self.output_columns],
            "projections": [asdict(item) for item in self.projections],
            "kind": self.kind.value,
            "require_all_right_rows": self.require_all_right_rows,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExactJoinRule":
        return cls(
            rule_id=str(payload["rule_id"]),
            output_dataset_name=str(payload["output_dataset_name"]),
            left_dataset_id=str(payload["left_dataset_id"]),
            right_dataset_id=str(payload["right_dataset_id"]),
            keys=tuple(JoinKey(**dict(item)) for item in payload.get("keys", ())),
            output_columns=tuple(
                StructuralOutputColumn(**dict(item))
                for item in payload.get("output_columns", ())
            ),
            projections=tuple(
                StructuralProjection(**dict(item))
                for item in payload.get("projections", ())
            ),
            kind=JoinKind(str(payload.get("kind", "LEFT"))),
            require_all_right_rows=bool(payload.get("require_all_right_rows", True)),
        )


@dataclass(frozen=True, slots=True)
class UnionBranch:
    source_dataset_id: str
    projections: tuple[StructuralProjection, ...]

    def __post_init__(self) -> None:
        _text(self.source_dataset_id, "union source dataset", 200)
        if not self.projections or any(
            item.source_dataset_id != self.source_dataset_id
            for item in self.projections
        ):
            raise ValueError("Union branch projections must use their branch dataset")


@dataclass(frozen=True, slots=True)
class UnionAllRule:
    rule_id: str
    output_dataset_name: str
    output_columns: tuple[StructuralOutputColumn, ...]
    branches: tuple[UnionBranch, ...]

    def __post_init__(self) -> None:
        _uuid(self.rule_id, "union rule ID")
        _dataset_name(self.output_dataset_name)
        if not 2 <= len(self.branches) <= MAX_STRUCTURAL_BRANCHES:
            raise ValueError("Union-all requires two to ten branches")
        if len({item.source_dataset_id for item in self.branches}) != len(self.branches):
            raise ValueError("Union-all branch datasets must be unique")
        for branch in self.branches:
            _validate_output(self.output_columns, branch.projections)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "output_dataset_name": self.output_dataset_name,
            "output_columns": [asdict(item) for item in self.output_columns],
            "branches": [
                {
                    "source_dataset_id": item.source_dataset_id,
                    "projections": [asdict(projection) for projection in item.projections],
                }
                for item in self.branches
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "UnionAllRule":
        return cls(
            rule_id=str(payload["rule_id"]),
            output_dataset_name=str(payload["output_dataset_name"]),
            output_columns=tuple(
                StructuralOutputColumn(**dict(item))
                for item in payload.get("output_columns", ())
            ),
            branches=tuple(
                UnionBranch(
                    source_dataset_id=str(item["source_dataset_id"]),
                    projections=tuple(
                        StructuralProjection(**dict(projection))
                        for projection in item.get("projections", ())
                    ),
                )
                for item in payload.get("branches", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class GroupKey:
    output_column_key: str
    source_column_key: str
    value_type: str = "string"

    def __post_init__(self) -> None:
        _column_key(self.output_column_key)
        _column_key(self.source_column_key)
        if self.value_type not in _VALUE_TYPES:
            raise ValueError("Group-key type is unsupported")


@dataclass(frozen=True, slots=True)
class AggregateSpec:
    output_column_key: str
    operation: AggregateOperation
    source_column_key: str | None = None
    unit: str = ""

    def __post_init__(self) -> None:
        _column_key(self.output_column_key)
        object.__setattr__(self, "operation", AggregateOperation(self.operation))
        if self.operation is AggregateOperation.SUM:
            if self.source_column_key is None:
                raise ValueError("SUM requires one source column")
            _column_key(self.source_column_key)
        elif self.source_column_key is not None:
            raise ValueError("COUNT does not accept a source column")
        if len(self.unit.strip()) > 40:
            raise ValueError("Aggregate unit is too long")


@dataclass(frozen=True, slots=True)
class GroupAggregateRule:
    rule_id: str
    output_dataset_name: str
    source_dataset_id: str
    output_columns: tuple[StructuralOutputColumn, ...]
    group_keys: tuple[GroupKey, ...]
    aggregates: tuple[AggregateSpec, ...]

    def __post_init__(self) -> None:
        _uuid(self.rule_id, "group rule ID")
        _dataset_name(self.output_dataset_name)
        _text(self.source_dataset_id, "group source dataset", 200)
        if not 1 <= len(self.group_keys) <= MAX_STRUCTURAL_KEYS:
            raise ValueError("Grouping requires one to five keys")
        if not self.aggregates:
            raise ValueError("Grouping requires at least one aggregate")
        output_keys = {item.column_key for item in self.output_columns}
        declared = {
            *(item.output_column_key for item in self.group_keys),
            *(item.output_column_key for item in self.aggregates),
        }
        if output_keys != declared or len(output_keys) != len(self.output_columns):
            raise ValueError("Group outputs must match keys and aggregates exactly")

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "output_dataset_name": self.output_dataset_name,
            "source_dataset_id": self.source_dataset_id,
            "output_columns": [asdict(item) for item in self.output_columns],
            "group_keys": [asdict(item) for item in self.group_keys],
            "aggregates": [
                {**asdict(item), "operation": item.operation.value}
                for item in self.aggregates
            ],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GroupAggregateRule":
        return cls(
            rule_id=str(payload["rule_id"]),
            output_dataset_name=str(payload["output_dataset_name"]),
            source_dataset_id=str(payload["source_dataset_id"]),
            output_columns=tuple(
                StructuralOutputColumn(**dict(item))
                for item in payload.get("output_columns", ())
            ),
            group_keys=tuple(
                GroupKey(**dict(item)) for item in payload.get("group_keys", ())
            ),
            aggregates=tuple(
                AggregateSpec(**dict(item)) for item in payload.get("aggregates", ())
            ),
        )


StructuralRule = ExactJoinRule | UnionAllRule | GroupAggregateRule
PhysicalLineage = Mapping[str, tuple[int, ...]]


@dataclass(frozen=True, slots=True)
class StructuralReconciliation:
    input_rows: int
    output_rows: int
    lineage_links: int
    matched_left_rows: int = 0
    unmatched_left_rows: int = 0
    matched_right_rows: int = 0
    unmatched_right_rows: int = 0

    def __post_init__(self) -> None:
        values = (
            self.input_rows,
            self.output_rows,
            self.lineage_links,
            self.matched_left_rows,
            self.unmatched_left_rows,
            self.matched_right_rows,
            self.unmatched_right_rows,
        )
        if any(item < 0 for item in values):
            raise ValueError("Structural reconciliation counts cannot be negative")
        if self.output_rows and self.lineage_links < self.output_rows:
            raise ValueError("Every structural output requires lineage")


@dataclass(frozen=True, slots=True)
class StructuralOutput:
    dataset: SourceDataset
    table: SourceTable
    lineage: Mapping[int, PhysicalLineage]
    reconciliation: StructuralReconciliation

    def __post_init__(self) -> None:
        if self.dataset.name != self.table.dataset:
            raise ValueError("Structural dataset and table names differ")
        if self.dataset.row_count != len(self.table.rows):
            raise ValueError("Structural dataset row count is inconsistent")
        if set(self.lineage) != {item.number for item in self.table.rows}:
            raise ValueError("Every structural row requires physical lineage")


@dataclass(frozen=True, slots=True)
class StructuralExecution:
    outputs: tuple[StructuralOutput, ...]

    def by_dataset_id(self) -> dict[str, StructuralOutput]:
        return {item.dataset.dataset_id: item for item in self.outputs}


def structural_dataset_id(rule: StructuralRule) -> str:
    identity = uuid5(
        NAMESPACE_URL,
        f"urn:impodo:structural:{content_hash(rule.to_dict())}",
    )
    return f"structural:{identity}"


def structural_mapping_selection(
    selection: SourceSelection,
    rules: Iterable[StructuralRule],
) -> SourceSelection:
    """Add deterministic structural output shapes to the mapping selection."""

    rule_set = tuple(rules)
    available = {item.dataset_id: item for item in selection.datasets}
    outputs: list[SourceDataset] = []
    pending = list(rule_set)
    while pending:
        progressed = False
        for rule in tuple(sorted(pending, key=lambda item: item.rule_id)):
            source_ids = _source_ids(rule)
            if not source_ids.issubset(available):
                continue
            dataset = _output_dataset(rule, available, row_count=_estimated_rows(rule, available))
            if dataset.dataset_id in available or dataset.name in {
                item.name for item in available.values()
            }:
                raise StructuralError("Structural output dataset is not unique")
            available[dataset.dataset_id] = dataset
            outputs.append(dataset)
            pending.remove(rule)
            progressed = True
        if not progressed:
            raise StructuralError("Structural rules contain a missing input or dependency cycle")
    if not outputs:
        return selection
    datasets = (*selection.datasets, *outputs)
    return replace(
        selection,
        datasets=datasets,
        content_hash=content_hash(
            {
                "source_selection_hash": selection.content_hash,
                "structural_rules": [item.to_dict() for item in sorted(rule_set, key=lambda item: item.rule_id)],
                "datasets": [item.dataset_id for item in datasets],
            }
        ),
    )


def execute_structural_rules(
    *,
    selection: SourceSelection,
    loaded_tables: Mapping[str, SourceTable],
    rules: Iterable[StructuralRule],
) -> StructuralExecution:
    """Execute a dependency-ordered set of bounded structural rules."""

    available_specs = {item.dataset_id: item for item in selection.datasets}
    available_tables = dict(loaded_tables)
    lineage: dict[str, Mapping[int, PhysicalLineage]] = {
        dataset_id: {
            row.number: {dataset_id: (row.number,)} for row in table.rows
        }
        for dataset_id, table in loaded_tables.items()
    }
    if set(available_tables) != set(available_specs):
        raise StructuralError("Loaded structural inputs do not match the frozen selection")
    outputs: list[StructuralOutput] = []
    pending = list(rules)
    while pending:
        progressed = False
        for rule in tuple(sorted(pending, key=lambda item: item.rule_id)):
            source_ids = _source_ids(rule)
            if not source_ids.issubset(available_tables):
                continue
            if isinstance(rule, ExactJoinRule):
                table, output_lineage, reconciliation = _execute_join(
                    rule,
                    available_specs,
                    available_tables,
                    lineage,
                )
            elif isinstance(rule, UnionAllRule):
                table, output_lineage, reconciliation = _execute_union(
                    rule,
                    available_specs,
                    available_tables,
                    lineage,
                )
            else:
                table, output_lineage, reconciliation = _execute_group(
                    rule,
                    available_specs,
                    available_tables,
                    lineage,
                )
            dataset = _output_dataset(rule, available_specs, row_count=len(table.rows))
            output = StructuralOutput(dataset, table, output_lineage, reconciliation)
            dataset_id = dataset.dataset_id
            available_specs[dataset_id] = dataset
            available_tables[dataset_id] = table
            lineage[dataset_id] = output_lineage
            outputs.append(output)
            pending.remove(rule)
            progressed = True
        if not progressed:
            raise StructuralError("Structural rules contain a missing input or dependency cycle")
    return StructuralExecution(tuple(outputs))


def _execute_join(rule, specs, tables, lineage):
    left_spec = specs[rule.left_dataset_id]
    right_spec = specs[rule.right_dataset_id]
    left_table = tables[rule.left_dataset_id]
    right_table = tables[rule.right_dataset_id]
    right_index: dict[tuple[Any, ...], SourceRow] = {}
    for row in right_table.rows:
        key = _join_key(row, right_spec, rule.keys, side="right")
        if key in right_index:
            raise StructuralError("Join right-side keys are not unique")
        right_index[key] = row
    projections = {item.output_column_key: item for item in rule.projections}
    rows: list[SourceRow] = []
    output_lineage: dict[int, PhysicalLineage] = {}
    matched_right: set[int] = set()
    unmatched_left = 0
    for left in left_table.rows:
        key = _join_key(left, left_spec, rule.keys, side="left")
        right = right_index.get(key)
        if right is None:
            unmatched_left += 1
            if rule.kind is JoinKind.INNER:
                raise StructuralError("Inner join has unmatched left rows")
        else:
            matched_right.add(right.number)
        values: dict[str, Any] = {}
        for column in rule.output_columns:
            projection = projections[column.column_key]
            if projection.source_dataset_id == rule.left_dataset_id:
                values[column.source_name] = _value(left, left_spec, projection.source_column_key)
            else:
                values[column.source_name] = (
                    _value(right, right_spec, projection.source_column_key)
                    if right is not None
                    else None
                )
        number = len(rows) + 1
        rows.append(SourceRow(number, values))
        sources = [lineage[rule.left_dataset_id][left.number]]
        if right is not None:
            sources.append(lineage[rule.right_dataset_id][right.number])
        output_lineage[number] = _merge_lineage(sources)
    unmatched_right = len(right_table.rows) - len(matched_right)
    if rule.require_all_right_rows and unmatched_right:
        raise StructuralError("Join has unrepresented right-side rows")
    table = _table(rule, rows, _headers(rule.output_columns), tables)
    return table, output_lineage, StructuralReconciliation(
        input_rows=len(left_table.rows) + len(right_table.rows),
        output_rows=len(rows),
        lineage_links=sum(
            sum(len(source_rows) for source_rows in item.values())
            for item in output_lineage.values()
        ),
        matched_left_rows=len(left_table.rows) - unmatched_left,
        unmatched_left_rows=unmatched_left,
        matched_right_rows=len(matched_right),
        unmatched_right_rows=unmatched_right,
    )


def _execute_union(rule, specs, tables, lineage):
    rows: list[SourceRow] = []
    output_lineage: dict[int, PhysicalLineage] = {}
    for branch in rule.branches:
        source_spec = specs[branch.source_dataset_id]
        projections = {item.output_column_key: item for item in branch.projections}
        for source_row in tables[branch.source_dataset_id].rows:
            number = len(rows) + 1
            rows.append(
                SourceRow(
                    number,
                    {
                        column.source_name: _value(
                            source_row,
                            source_spec,
                            projections[column.column_key].source_column_key,
                        )
                        for column in rule.output_columns
                    },
                )
            )
            output_lineage[number] = lineage[branch.source_dataset_id][source_row.number]
    table = _table(rule, rows, _headers(rule.output_columns), tables)
    return table, output_lineage, StructuralReconciliation(
        input_rows=sum(len(tables[item.source_dataset_id].rows) for item in rule.branches),
        output_rows=len(rows),
        lineage_links=sum(
            sum(len(source_rows) for source_rows in item.values())
            for item in output_lineage.values()
        ),
    )


def _execute_group(rule, specs, tables, lineage):
    source_spec = specs[rule.source_dataset_id]
    source_table = tables[rule.source_dataset_id]
    groups: dict[tuple[Any, ...], list[SourceRow]] = {}
    for row in source_table.rows:
        try:
            key = tuple(
                parse_value(
                    _value(row, source_spec, item.source_column_key),
                    item.value_type,
                    NormalizationSpec(),
                    required=True,
                )
                for item in rule.group_keys
            )
        except ValueParseError as error:
            raise StructuralError("Grouping key is blank or invalid") from error
        groups.setdefault(key, []).append(row)
    columns = {item.column_key: item for item in rule.output_columns}
    rows: list[SourceRow] = []
    output_lineage: dict[int, PhysicalLineage] = {}
    for key, source_rows in sorted(groups.items(), key=lambda item: repr(portable_value(item[0]))):
        values: dict[str, Any] = {}
        for index, group_key in enumerate(rule.group_keys):
            values[columns[group_key.output_column_key].source_name] = key[index]
        for aggregate in rule.aggregates:
            output_name = columns[aggregate.output_column_key].source_name
            if aggregate.operation is AggregateOperation.COUNT:
                values[output_name] = len(source_rows)
            else:
                total = Decimal("0")
                for source_row in source_rows:
                    raw = _value(source_row, source_spec, aggregate.source_column_key or "")
                    try:
                        value = Decimal(str(raw))
                    except (InvalidOperation, TypeError) as error:
                        raise StructuralError("SUM contains a non-decimal value") from error
                    if not value.is_finite():
                        raise StructuralError("SUM contains a non-finite value")
                    total += value
                values[output_name] = total
        number = len(rows) + 1
        rows.append(SourceRow(number, values))
        output_lineage[number] = _merge_lineage(
            lineage[rule.source_dataset_id][item.number] for item in source_rows
        )
    table = _table(rule, rows, _headers(rule.output_columns), tables)
    return table, output_lineage, StructuralReconciliation(
        input_rows=len(source_table.rows),
        output_rows=len(rows),
        lineage_links=sum(
            sum(len(source_rows) for source_rows in item.values())
            for item in output_lineage.values()
        ),
    )


def _join_key(row, dataset, keys, *, side):
    result = []
    for key in keys:
        column_key = key.left_column_key if side == "left" else key.right_column_key
        try:
            result.append(
                parse_value(
                    _value(row, dataset, column_key),
                    key.value_type,
                    NormalizationSpec(),
                    required=True,
                )
            )
        except ValueParseError as error:
            raise StructuralError("Join key is blank or invalid") from error
    return tuple(result)


def _value(row: SourceRow | None, dataset: SourceDataset, column_key: str) -> Any:
    if row is None:
        return None
    column = next((item for item in dataset.columns if item.stable_key == column_key), None)
    if column is None:
        raise StructuralError("Structural rule references a missing column")
    return row.values.get(column.source_name)


def _merge_lineage(items: Iterable[PhysicalLineage]) -> dict[str, tuple[int, ...]]:
    result: dict[str, set[int]] = {}
    for item in items:
        for dataset_id, source_rows in item.items():
            result.setdefault(dataset_id, set()).update(source_rows)
    return {
        dataset_id: tuple(sorted(source_rows))
        for dataset_id, source_rows in sorted(result.items())
    }


def _table(rule, rows, headers, tables):
    source_hashes = {
        dataset_id: tables[dataset_id].content_hash for dataset_id in _source_ids(rule)
    }
    return SourceTable(
        dataset=rule.output_dataset_name,
        path=PurePosixPath(f"structural/{structural_dataset_id(rule)}"),
        headers=headers,
        rows=tuple(rows),
        content_hash=content_hash(
            {
                "rule": rule.to_dict(),
                "sources": source_hashes,
            }
        ),
    )


def _output_dataset(rule, available, *, row_count):
    sources = [available[item] for item in sorted(_source_ids(rule))]
    source_hashes = {
        item.dataset_id: item.source_evidence_hash for item in sources
    }
    return SourceDataset(
        dataset_id=structural_dataset_id(rule),
        name=rule.output_dataset_name,
        source=DerivedSourceBinding(
            rule_hash=content_hash(rule.to_dict()),
            input_dataset_ids=tuple(item.dataset_id for item in sources),
            data_hash=content_hash(
                {
                    "rule": rule.to_dict(),
                    "sources": source_hashes,
                }
            ),
        ),
        row_count=row_count,
        columns=tuple(
            SourceDatasetColumn(
                ordinal=index,
                source_name=item.source_name,
                stable_key=item.column_key,
                candidate_type=item.candidate_type,
            )
            for index, item in enumerate(rule.output_columns, 1)
        ),
    )


def _estimated_rows(rule, available):
    if isinstance(rule, ExactJoinRule):
        return available[rule.left_dataset_id].row_count
    if isinstance(rule, UnionAllRule):
        return sum(available[item.source_dataset_id].row_count for item in rule.branches)
    return available[rule.source_dataset_id].row_count


def _source_ids(rule):
    if isinstance(rule, ExactJoinRule):
        return {rule.left_dataset_id, rule.right_dataset_id}
    if isinstance(rule, UnionAllRule):
        return {item.source_dataset_id for item in rule.branches}
    return {rule.source_dataset_id}


def _headers(columns):
    return tuple(item.source_name for item in columns)


def _validate_output(columns, projections):
    if not columns or len(columns) > MAX_STRUCTURAL_COLUMNS:
        raise ValueError("Structural output columns are outside the supported bound")
    keys = [item.column_key for item in columns]
    projection_keys = [item.output_column_key for item in projections]
    if len(set(keys)) != len(keys) or set(keys) != set(projection_keys):
        raise ValueError("Every structural output requires one projection")
    if len(set(projection_keys)) != len(projection_keys):
        raise ValueError("Structural output projections must be unique")


def _column_key(value):
    _text(value, "structural column key", 500)


def _dataset_name(value):
    clean = _text(value, "structural dataset name", 63)
    if not clean[0].isalpha() or not clean.replace("_", "").isalnum() or clean != clean.casefold():
        raise ValueError("Structural dataset name must use lowercase letters, digits, and underscores")


def _text(value, label, maximum):
    clean = str(value).strip()
    if not clean:
        raise ValueError(f"{label} is required")
    if len(clean) > maximum:
        raise ValueError(f"{label} is too long")
    return clean


def _uuid(value, label):
    try:
        UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"{label} is invalid") from error
