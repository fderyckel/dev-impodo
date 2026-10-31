"""Parse and present the bounded exact-join browser form.

Layer: web form adapter. Network values remain inert until this module resolves
them against the current frozen source selection and saved structural outputs.
The application service still owns the complete-data review and atomic save.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TypedDict
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from starlette.datastructures import FormData

from impodo.application.workspace.derived_entities import (
    ExactJoinGroupAddition,
    ExactJoinGroupProjection,
    ExactJoinGroupRequest,
    ExactJoinRequest,
)
from impodo.domain.structural import (
    MAX_STRUCTURAL_COLUMNS,
    STRUCTURAL_OUTPUT_VALUE_TYPES,
    ExactJoinReview,
    ExactJoinRule,
    GroupAggregateRule,
    JoinKey,
    JoinKind,
    StructuralOutputColumn,
    StructuralProjection,
    UnionAllRule,
    structural_dataset_id,
    structural_mapping_selection,
)
from impodo.domain.workspace.contracts import (
    SourceDataset,
    SourceDatasetColumn,
    SourceSelection,
)
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    SourceDatasetUse,
    source_dataset_uses_for_authoring,
    validate_source_dataset_uses,
)
from impodo.domain.workspace.errors import WorkspaceError

from .forms import _checked, _text

EXACT_JOIN_FORM_FIELDS = {
    "csrf_token",
    "expected_parent_version",
    "rule_id",
    "output_dataset_name",
    "left_dataset_id",
    "right_dataset_id",
    "kind",
    "require_all_right_rows",
    "left_dataset_use",
    "right_dataset_use",
    "projection",
    "projection_name",
    *{
        f"key_{side}_{index}"
        for index in range(1, 6)
        for side in ("left", "right", "type")
    },
}
_MAX_GROUP_DATASET_CANDIDATES = 100
EXACT_JOIN_GROUP_FORM_FIELDS = {
    "csrf_token",
    "expected_parent_version",
    "group_id",
    "output_dataset_name",
    "primary_dataset_id",
    "group_addition",
    "group_projection",
    "group_projection_name",
    *{
        f"primary_key_{part}_{index}"
        for index in range(1, 6)
        for part in ("field", "type")
    },
    *{
        field
        for dataset_index in range(_MAX_GROUP_DATASET_CANDIDATES)
        for field in (
            f"group_addition_kind_{dataset_index}",
            f"group_addition_require_all_{dataset_index}",
            *(
                f"group_addition_key_{dataset_index}_{key_index}"
                for key_index in range(1, 6)
            ),
        )
    },
}
_JOIN_VALUE_TYPES = frozenset(
    {"string", "integer", "decimal", "boolean", "date", "datetime"}
)
_MAX_PROJECTION_CANDIDATES = 1_000


class SavedExactJoinView(TypedDict):
    """Typed presentation shape for one saved exact-join rule."""

    rule: ExactJoinRule
    review: ExactJoinReview | None
    left: SourceDataset
    right: SourceDataset
    output: SourceDataset
    left_use: SourceDatasetUse | None
    right_use: SourceDatasetUse | None
    step: int
    inputs: tuple[SourceDataset, ...]
    grouped: bool
    columns: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class ExactJoinFormSubmission:
    """Retain one parsed browser submission without trusting source identities."""

    expected_parent_version: int | None
    rule_id: str
    output_dataset_name: str
    left_dataset_id: str
    right_dataset_id: str
    key_rows: tuple[tuple[str, str, str], ...]
    kind: str
    require_all_right_rows: bool
    left_dataset_use: str
    right_dataset_use: str
    projection_indexes: tuple[int, ...]
    projection_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExactJoinGroupAdditionSubmission:
    """Retain one indexed added-table row from the grouped browser form."""

    dataset_index: int
    key_column_keys: tuple[str, ...]
    kind: str
    require_all_rows: bool


@dataclass(frozen=True, slots=True)
class ExactJoinGroupFormSubmission:
    """Retain a bounded grouped form without trusting its dataset indexes."""

    expected_parent_version: int | None
    group_id: str
    output_dataset_name: str
    primary_dataset_id: str
    primary_key_rows: tuple[tuple[str, str], ...]
    additions: tuple[ExactJoinGroupAdditionSubmission, ...]
    projection_indexes: tuple[int, ...]
    projection_names: tuple[str, ...]


def parse_exact_join_form(form: FormData) -> ExactJoinFormSubmission:
    """Parse bounded scalar and repeated values outside a database scope."""

    raw_version = _text(form, "expected_parent_version")
    try:
        expected_parent_version = int(raw_version) if raw_version else None
    except ValueError as error:
        raise ValueError("The source-preparation version is invalid") from error

    rule_id = _text(form, "rule_id") or str(uuid4())
    try:
        rule_id = str(UUID(rule_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("The combination review identifier is invalid") from error

    raw_projection_values = tuple(form.getlist("projection"))
    if not all(isinstance(value, str) for value in raw_projection_values):
        raise ValueError("The selected output fields are invalid")
    raw_projection_indexes = tuple(
        value for value in raw_projection_values if isinstance(value, str)
    )
    if len(raw_projection_indexes) > MAX_STRUCTURAL_COLUMNS:
        raise ValueError(f"Choose no more than {MAX_STRUCTURAL_COLUMNS} output fields")
    try:
        projection_indexes = tuple(int(value) for value in raw_projection_indexes)
    except (TypeError, ValueError) as error:
        raise ValueError("The selected output fields are invalid") from error
    if len(set(projection_indexes)) != len(projection_indexes) or any(
        index < 0 or index >= _MAX_PROJECTION_CANDIDATES for index in projection_indexes
    ):
        raise ValueError("The selected output fields are invalid")

    projection_names = tuple(
        value if isinstance(value, str) else ""
        for value in form.getlist("projection_name")
    )
    if len(projection_names) > _MAX_PROJECTION_CANDIDATES:
        raise ValueError("This source contains too many fields for browser review")

    return ExactJoinFormSubmission(
        expected_parent_version=expected_parent_version,
        rule_id=rule_id,
        output_dataset_name=_text(form, "output_dataset_name"),
        left_dataset_id=_text(form, "left_dataset_id"),
        right_dataset_id=_text(form, "right_dataset_id"),
        key_rows=tuple(
            (
                _text(form, f"key_left_{index}"),
                _text(form, f"key_right_{index}"),
                _text(form, f"key_type_{index}") or "string",
            )
            for index in range(1, 6)
        ),
        kind=_text(form, "kind") or JoinKind.LEFT.value,
        require_all_right_rows=_checked(form, "require_all_right_rows"),
        left_dataset_use=(
            _text(form, "left_dataset_use") or SourceDatasetUse.PREPARATION_ONLY.value
        ),
        right_dataset_use=(
            _text(form, "right_dataset_use") or SourceDatasetUse.PREPARATION_ONLY.value
        ),
        projection_indexes=projection_indexes,
        projection_names=projection_names,
    )


def parse_exact_join_group_form(form: FormData) -> ExactJoinGroupFormSubmission:
    """Parse one complete multi-table form outside the local database scope."""

    raw_version = _text(form, "expected_parent_version")
    try:
        expected_parent_version = int(raw_version) if raw_version else None
    except ValueError as error:
        raise ValueError("The source-preparation version is invalid") from error
    group_id = _text(form, "group_id") or str(uuid4())
    try:
        group_id = str(UUID(group_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("The combination review identifier is invalid") from error

    raw_additions = tuple(form.getlist("group_addition"))
    if not all(isinstance(value, str) for value in raw_additions):
        raise ValueError("The selected added tables are invalid")
    try:
        addition_indexes = tuple(int(str(value)) for value in raw_additions)
    except ValueError as error:
        raise ValueError("The selected added tables are invalid") from error
    if (
        not addition_indexes
        or len(addition_indexes) > 9
        or len(set(addition_indexes)) != len(addition_indexes)
        or any(
            index < 0 or index >= _MAX_GROUP_DATASET_CANDIDATES
            for index in addition_indexes
        )
    ):
        raise ValueError("Choose between one and nine added tables")

    raw_projections = tuple(form.getlist("group_projection"))
    if not all(isinstance(value, str) for value in raw_projections):
        raise ValueError("The selected output fields are invalid")
    try:
        projection_indexes = tuple(int(str(value)) for value in raw_projections)
    except ValueError as error:
        raise ValueError("The selected output fields are invalid") from error
    if len(projection_indexes) > MAX_STRUCTURAL_COLUMNS:
        raise ValueError(
            f"Choose no more than {MAX_STRUCTURAL_COLUMNS} output fields"
        )
    if len(set(projection_indexes)) != len(projection_indexes) or any(
        index < 0 or index >= _MAX_PROJECTION_CANDIDATES
        for index in projection_indexes
    ):
        raise ValueError("The selected output fields are invalid")
    projection_names = tuple(
        value if isinstance(value, str) else ""
        for value in form.getlist("group_projection_name")
    )
    if len(projection_names) > _MAX_PROJECTION_CANDIDATES:
        raise ValueError("This source contains too many fields for browser review")

    return ExactJoinGroupFormSubmission(
        expected_parent_version=expected_parent_version,
        group_id=group_id,
        output_dataset_name=_text(form, "output_dataset_name"),
        primary_dataset_id=_text(form, "primary_dataset_id"),
        primary_key_rows=tuple(
            (
                _text(form, f"primary_key_field_{index}"),
                _text(form, f"primary_key_type_{index}") or "string",
            )
            for index in range(1, 6)
        ),
        additions=tuple(
            ExactJoinGroupAdditionSubmission(
                dataset_index=index,
                key_column_keys=tuple(
                    _text(form, f"group_addition_key_{index}_{key_index}")
                    for key_index in range(1, 6)
                ),
                kind=(
                    _text(form, f"group_addition_kind_{index}")
                    or JoinKind.LEFT.value
                ),
                require_all_rows=_checked(
                    form,
                    f"group_addition_require_all_{index}",
                ),
            )
            for index in addition_indexes
        ),
        projection_indexes=projection_indexes,
        projection_names=projection_names,
    )


def retain_exact_join_group_form(form: FormData) -> ExactJoinGroupFormSubmission:
    """Retain bounded inert choices when strict grouped-form parsing fails."""

    raw_version = _text(form, "expected_parent_version")
    try:
        expected_parent_version = int(raw_version) if raw_version else None
    except ValueError:
        expected_parent_version = None
    raw_group_id = _text(form, "group_id")
    try:
        group_id = str(UUID(raw_group_id))
    except (ValueError, TypeError, AttributeError):
        group_id = str(uuid4())

    addition_indexes: list[int] = []
    for value in form.getlist("group_addition")[:9]:
        try:
            index = int(str(value))
        except ValueError:
            continue
        if (
            0 <= index < _MAX_GROUP_DATASET_CANDIDATES
            and index not in addition_indexes
        ):
            addition_indexes.append(index)
    projection_indexes: list[int] = []
    for value in form.getlist("group_projection")[:_MAX_PROJECTION_CANDIDATES]:
        try:
            index = int(str(value))
        except ValueError:
            continue
        if 0 <= index < _MAX_PROJECTION_CANDIDATES and index not in projection_indexes:
            projection_indexes.append(index)

    return ExactJoinGroupFormSubmission(
        expected_parent_version=expected_parent_version,
        group_id=group_id,
        output_dataset_name=_text(form, "output_dataset_name"),
        primary_dataset_id=_text(form, "primary_dataset_id"),
        primary_key_rows=tuple(
            (
                _text(form, f"primary_key_field_{index}"),
                _text(form, f"primary_key_type_{index}") or "string",
            )
            for index in range(1, 6)
        ),
        additions=tuple(
            ExactJoinGroupAdditionSubmission(
                dataset_index=index,
                key_column_keys=tuple(
                    _text(form, f"group_addition_key_{index}_{key_index}")
                    for key_index in range(1, 6)
                ),
                kind=_text(form, f"group_addition_kind_{index}") or JoinKind.LEFT.value,
                require_all_rows=_checked(
                    form,
                    f"group_addition_require_all_{index}",
                ),
            )
            for index in addition_indexes
        ),
        projection_indexes=tuple(projection_indexes),
        projection_names=tuple(
            value if isinstance(value, str) else ""
            for value in form.getlist("group_projection_name")[
                :_MAX_PROJECTION_CANDIDATES
            ]
        ),
    )


def available_exact_join_selection(
    selection: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> SourceSelection:
    """Return physical inputs plus dependency-ordered structural outputs."""

    structural_rules = tuple(
        rule
        for rule in (plan.rules if plan else ())
        if isinstance(rule, (ExactJoinRule, UnionAllRule, GroupAggregateRule))
    )
    available = structural_mapping_selection(selection, structural_rules)
    hidden_ids = {
        structural_dataset_id(rule)
        for rule in structural_rules
        if isinstance(rule, ExactJoinRule) and not rule.map_output
    }
    if not hidden_ids:
        return available
    return replace(
        available,
        datasets=tuple(
            dataset
            for dataset in available.datasets
            if dataset.dataset_id not in hidden_ids
        ),
    )


def exact_join_projection_candidates(
    selection: SourceSelection,
) -> tuple[tuple[SourceDataset, SourceDatasetColumn], ...]:
    """Return the stable browser ordering for selectable output fields."""

    candidates = tuple(
        (dataset, column)
        for dataset in selection.datasets
        for column in dataset.columns
    )
    if len(candidates) > _MAX_PROJECTION_CANDIDATES:
        raise ValueError("This source contains too many fields for browser review")
    return candidates


def default_exact_join_form(
    selection: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> ExactJoinFormSubmission:
    """Build safe defaults for the current exact-join authoring page."""

    available = available_exact_join_selection(selection, plan)
    candidates = exact_join_projection_candidates(available)
    datasets = available.datasets
    return ExactJoinFormSubmission(
        expected_parent_version=plan.version if plan else None,
        rule_id=str(uuid4()),
        output_dataset_name="",
        left_dataset_id=datasets[0].dataset_id if datasets else "",
        right_dataset_id=datasets[1].dataset_id if len(datasets) > 1 else "",
        key_rows=tuple(("", "", "string") for _index in range(5)),
        kind=JoinKind.LEFT.value,
        require_all_right_rows=False,
        left_dataset_use=SourceDatasetUse.PREPARATION_ONLY.value,
        right_dataset_use=SourceDatasetUse.PREPARATION_ONLY.value,
        projection_indexes=(),
        projection_names=tuple(column.source_name for _dataset, column in candidates),
    )


def exact_join_group_projection_candidates(
    selection: SourceSelection,
) -> tuple[tuple[SourceDataset, SourceDatasetColumn], ...]:
    """Return bounded physical-field candidates for the grouped normal path."""

    if len(selection.datasets) > _MAX_GROUP_DATASET_CANDIDATES:
        raise ValueError(
            "This source contains too many tables for grouped browser review"
        )
    return exact_join_projection_candidates(selection)


def default_exact_join_group_form(
    selection: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> ExactJoinGroupFormSubmission:
    """Build safe grouped-form defaults for one current frozen selection."""

    candidates = exact_join_group_projection_candidates(selection)
    primary_dataset_id = (
        selection.datasets[0].dataset_id if selection.datasets else ""
    )
    name_counts: dict[str, int] = {}
    for _dataset, column in candidates:
        label = column.source_name.casefold()
        name_counts[label] = name_counts.get(label, 0) + 1
    projection_names = tuple(
        (
            qualified_exact_join_output_name(dataset, column)
            if (
                dataset.dataset_id != primary_dataset_id
                and name_counts[column.source_name.casefold()] > 1
            )
            else column.source_name
        )
        for dataset, column in candidates
    )
    return ExactJoinGroupFormSubmission(
        expected_parent_version=plan.version if plan else None,
        group_id=str(uuid4()),
        output_dataset_name="",
        primary_dataset_id=primary_dataset_id,
        primary_key_rows=tuple(("", "string") for _index in range(5)),
        additions=(),
        projection_indexes=tuple(
            index
            for index, (dataset, column) in enumerate(candidates)
            if dataset.dataset_id == primary_dataset_id
            and column.candidate_type in STRUCTURAL_OUTPUT_VALUE_TYPES
            and column.candidate_type != "empty"
        ),
        projection_names=projection_names,
    )


def qualified_exact_join_output_name(
    dataset: SourceDataset,
    column: SourceDatasetColumn,
) -> str:
    """Qualify one collision without exceeding the structural label bound."""

    prefix = f"{dataset.name}."
    return prefix + column.source_name[: 200 - len(prefix)]


def saved_join_field_sources(
    plan: DerivedEntityPlan,
    rule: ExactJoinRule,
) -> dict[str, tuple[str, str]]:
    """Trace saved group fields back through private steps to their physical inputs."""

    private = {
        structural_dataset_id(item): item
        for item in plan.rules
        if isinstance(item, ExactJoinRule) and not item.map_output
    }

    def origin(dataset_id: str, column_key: str) -> tuple[str, str]:
        ancestor = private.get(dataset_id)
        if ancestor is None:
            return dataset_id, column_key
        projection = next(
            item
            for item in ancestor.projections
            if item.output_column_key == column_key
        )
        return origin(projection.source_dataset_id, projection.source_column_key)

    return {
        item.output_column_key: origin(item.source_dataset_id, item.source_column_key)
        for item in rule.projections
    }


def saved_exact_join_form(
    selection: SourceSelection,
    plan: DerivedEntityPlan,
    rule_id: str,
) -> ExactJoinFormSubmission | ExactJoinGroupFormSubmission:
    """Reopen an existing combination from persisted metadata, without reading rows."""

    rule = next(
        (
            item
            for item in plan.rules
            if isinstance(item, ExactJoinRule)
            and item.map_output
            and item.rule_id == rule_id
        ),
        None,
    )
    if rule is None:
        raise WorkspaceError(
            "This saved combination is no longer available; reload the page"
        )
    if plan.source_selection_hash != selection.content_hash:
        raise WorkspaceError(
            "The saved combination does not match the current source tables"
        )
    if rule.group_id is None:
        pair_default = default_exact_join_form(selection, plan)
        candidates = exact_join_projection_candidates(
            available_exact_join_selection(selection, plan)
        )
        uses = source_dataset_uses_for_authoring(selection, plan)
        selected = {
            (item.source_dataset_id, item.source_column_key): column.source_name
            for item in rule.projections
            for column in rule.output_columns
            if column.column_key == item.output_column_key
        }
        return replace(
            pair_default,
            rule_id=rule.rule_id,
            output_dataset_name=rule.output_dataset_name,
            left_dataset_id=rule.left_dataset_id,
            right_dataset_id=rule.right_dataset_id,
            key_rows=tuple(
                (key.left_column_key, key.right_column_key, key.value_type)
                for key in rule.keys
            )
            + tuple(("", "", "string") for _ in range(5 - len(rule.keys))),
            kind=rule.kind.value,
            require_all_right_rows=rule.require_all_right_rows,
            left_dataset_use=uses.get(
                rule.left_dataset_id, SourceDatasetUse.PREPARATION_ONLY
            ).value,
            right_dataset_use=uses.get(
                rule.right_dataset_id, SourceDatasetUse.PREPARATION_ONLY
            ).value,
            projection_indexes=tuple(
                i
                for i, (dataset, column) in enumerate(candidates)
                if (dataset.dataset_id, column.stable_key) in selected
            ),
            projection_names=tuple(
                selected.get(
                    (dataset.dataset_id, column.stable_key), column.source_name
                )
                for dataset, column in candidates
            ),
        )

    group_default = default_exact_join_group_form(selection, plan)
    steps = sorted(
        (
            item
            for item in plan.rules
            if isinstance(item, ExactJoinRule) and item.group_id == rule.group_id
        ),
        key=lambda item: item.group_step or 0,
    )
    indexes = {dataset.dataset_id: i for i, dataset in enumerate(selection.datasets)}
    sources = saved_join_field_sources(plan, rule)
    selected = {
        sources[column.column_key]: column.source_name for column in rule.output_columns
    }
    candidates = exact_join_group_projection_candidates(selection)
    first = steps[0]
    return replace(
        group_default,
        group_id=rule.group_id,
        output_dataset_name=rule.output_dataset_name,
        primary_dataset_id=first.left_dataset_id,
        primary_key_rows=tuple(
            (key.left_column_key, key.value_type) for key in first.keys
        )
        + tuple(("", "string") for _ in range(5 - len(first.keys))),
        additions=tuple(
            ExactJoinGroupAdditionSubmission(
                dataset_index=indexes[step.right_dataset_id],
                key_column_keys=tuple(key.right_column_key for key in step.keys)
                + tuple("" for _ in range(5 - len(step.keys))),
                kind=step.kind.value,
                require_all_rows=step.require_all_right_rows,
            )
            for step in steps
        ),
        projection_indexes=tuple(
            i
            for i, (dataset, column) in enumerate(candidates)
            if (dataset.dataset_id, column.stable_key) in selected
        ),
        projection_names=tuple(
            selected.get(
                (dataset.dataset_id, column.stable_key),
                group_default.projection_names[i],
            )
            for i, (dataset, column) in enumerate(candidates)
        ),
    )


def resolve_exact_join_group_request(
    submission: ExactJoinGroupFormSubmission,
    selection: SourceSelection,
) -> ExactJoinGroupRequest:
    """Resolve posted indexes and fields only against frozen physical metadata."""

    datasets = {item.dataset_id: item for item in selection.datasets}
    primary = datasets.get(submission.primary_dataset_id)
    if primary is None:
        raise ValueError("Choose a current physical primary table")
    primary_columns = {item.stable_key for item in primary.columns}
    primary_keys: list[str] = []
    key_types: list[str] = []
    saw_blank = False
    for raw_key, value_type in submission.primary_key_rows:
        if not raw_key:
            saw_blank = True
            continue
        if saw_blank:
            raise ValueError("Use the shared key rows in order without gaps")
        key = _resolved_column_key(raw_key, primary.dataset_id)
        if key not in primary_columns:
            raise ValueError("Choose primary key fields from the primary table")
        if value_type not in _JOIN_VALUE_TYPES:
            raise ValueError("Choose a supported shared-key type")
        primary_keys.append(key)
        key_types.append(value_type)
    if not primary_keys:
        raise ValueError("Choose at least one primary key field")

    additions: list[ExactJoinGroupAddition] = []
    for addition in submission.additions:
        if addition.dataset_index >= len(selection.datasets):
            raise ValueError("Reload the page before choosing added tables")
        dataset = selection.datasets[addition.dataset_index]
        if dataset.dataset_id == primary.dataset_id:
            raise ValueError("The primary table cannot also be an added table")
        columns = {item.stable_key for item in dataset.columns}
        resolved_keys: list[str] = []
        for key_index in range(len(primary_keys)):
            raw_key = addition.key_column_keys[key_index]
            if not raw_key:
                raise ValueError(
                    f"Confirm every shared key field for {dataset.name}"
                )
            key = _resolved_column_key(raw_key, dataset.dataset_id)
            if key not in columns:
                raise ValueError(
                    "Choose added-table key fields from the matching table"
                )
            resolved_keys.append(key)
        try:
            kind = JoinKind(addition.kind)
        except ValueError as error:
            raise ValueError("Choose optional or required enrichment") from error
        additions.append(
            ExactJoinGroupAddition(
                dataset_id=dataset.dataset_id,
                key_column_keys=tuple(resolved_keys),
                kind=kind,
                require_all_rows=addition.require_all_rows,
            )
        )

    candidates = exact_join_group_projection_candidates(selection)
    if len(submission.projection_names) != len(candidates):
        raise ValueError("Reload the page before choosing output fields")
    participant_ids = {
        primary.dataset_id,
        *(item.dataset_id for item in additions),
    }
    projections: list[ExactJoinGroupProjection] = []
    for index in submission.projection_indexes:
        if index >= len(candidates):
            raise ValueError("Reload the page before choosing output fields")
        dataset, column = candidates[index]
        if dataset.dataset_id not in participant_ids:
            continue
        output_name = submission.projection_names[index].strip()
        if not output_name:
            raise ValueError("Give every selected output field a name")
        projections.append(
            ExactJoinGroupProjection(
                source_dataset_id=dataset.dataset_id,
                source_column_key=column.stable_key,
                output_name=output_name,
            )
        )
    if not projections:
        raise ValueError("Choose at least one output field")
    return ExactJoinGroupRequest(
        group_id=submission.group_id,
        output_dataset_name=submission.output_dataset_name,
        primary_dataset_id=primary.dataset_id,
        primary_key_column_keys=tuple(primary_keys),
        key_value_types=tuple(key_types),
        additions=tuple(additions),
        projections=tuple(projections),
    )


def resolve_exact_join_request(
    submission: ExactJoinFormSubmission,
    selection: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> tuple[ExactJoinRequest, SourceSelection]:
    """Resolve a parsed form only against current, eligible source metadata."""

    available = available_exact_join_selection(selection, plan)
    datasets = {dataset.dataset_id: dataset for dataset in available.datasets}
    left = datasets.get(submission.left_dataset_id)
    right = datasets.get(submission.right_dataset_id)
    if left is None or right is None:
        raise ValueError("Choose two current source datasets")
    if left.dataset_id == right.dataset_id:
        raise ValueError("Choose two different source datasets")

    left_columns = {column.stable_key: column for column in left.columns}
    right_columns = {column.stable_key: column for column in right.columns}
    keys: list[JoinKey] = []
    for raw_left_key, raw_right_key, value_type in submission.key_rows:
        if not raw_left_key and not raw_right_key:
            continue
        if not raw_left_key or not raw_right_key:
            raise ValueError("Complete both fields for each join-key pair")
        left_key = _resolved_column_key(raw_left_key, left.dataset_id)
        right_key = _resolved_column_key(raw_right_key, right.dataset_id)
        if left_key not in left_columns or right_key not in right_columns:
            raise ValueError("Choose join-key fields from the selected datasets")
        if value_type not in _JOIN_VALUE_TYPES:
            raise ValueError("Choose a supported join-key type")
        keys.append(JoinKey(left_key, right_key, value_type))
    if not keys:
        raise ValueError("Choose at least one exact join-key pair")

    candidates = exact_join_projection_candidates(available)
    if len(submission.projection_names) != len(candidates):
        raise ValueError("Reload the page before choosing output fields")
    output_columns: list[StructuralOutputColumn] = []
    projections: list[StructuralProjection] = []
    for index in submission.projection_indexes:
        if index >= len(candidates):
            raise ValueError("Reload the page before choosing output fields")
        dataset, column = candidates[index]
        if dataset.dataset_id not in {left.dataset_id, right.dataset_id}:
            raise ValueError("Choose output fields only from the two input datasets")
        output_name = submission.projection_names[index].strip()
        if not output_name:
            raise ValueError("Give every selected output field a name")
        output_key = "column:join:" + str(
            uuid5(
                NAMESPACE_URL,
                f"urn:impodo:join-column:{dataset.dataset_id}:{column.stable_key}",
            )
        )
        output_columns.append(
            StructuralOutputColumn(
                output_key,
                output_name,
                column.candidate_type,
            )
        )
        projections.append(
            StructuralProjection(
                output_key,
                dataset.dataset_id,
                column.stable_key,
            )
        )
    if not output_columns:
        raise ValueError("Choose at least one output field")

    return (
        ExactJoinRequest(
            rule_id=submission.rule_id,
            output_dataset_name=submission.output_dataset_name,
            left_dataset_id=left.dataset_id,
            right_dataset_id=right.dataset_id,
            keys=tuple(keys),
            output_columns=tuple(output_columns),
            projections=tuple(projections),
            kind=JoinKind(submission.kind),
            require_all_right_rows=submission.require_all_right_rows,
        ),
        available,
    )


def _resolved_column_key(value: str, expected_dataset_id: str) -> str:
    """Resolve a browser dataset-and-column token without trusting its prefix."""

    if "|" not in value:
        return value
    dataset_id, column_key = value.split("|", 1)
    if dataset_id != expected_dataset_id:
        raise ValueError("Choose join-key fields from the selected datasets")
    return column_key


def exact_join_dataset_uses(
    submission: ExactJoinFormSubmission,
    request: ExactJoinRequest,
    selection: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> dict[str, SourceDatasetUse]:
    """Build a complete physical policy from visible input choices."""

    physical_ids = {dataset.dataset_id for dataset in selection.datasets}
    uses = source_dataset_uses_for_authoring(selection, plan)

    for dataset_id, raw_use in (
        (request.left_dataset_id, submission.left_dataset_use),
        (request.right_dataset_id, submission.right_dataset_use),
    ):
        if dataset_id not in physical_ids:
            continue
        try:
            use = SourceDatasetUse(raw_use)
        except ValueError as error:
            raise ValueError("Choose how each physical input will be used") from error
        if not use.is_preparation_input:
            raise ValueError(
                "A combined physical input must be Preparation only or Map and prepare"
            )
        uses[dataset_id] = use
    return uses


def saved_exact_join_views(
    selection: SourceSelection,
    plan: DerivedEntityPlan | None,
) -> tuple[SavedExactJoinView, ...]:
    """Describe saved exact joins without rereading their source rows."""

    if plan is None:
        return ()
    available = available_exact_join_selection(selection, plan)
    datasets = {dataset.dataset_id: dataset for dataset in available.datasets}
    reviews = {evidence.rule_id: evidence.review for evidence in plan.join_reviews}
    uses = validate_source_dataset_uses(selection, plan)
    physical_ids = {dataset.dataset_id for dataset in selection.datasets}
    hidden_by_output = {
        structural_dataset_id(rule): rule
        for rule in plan.rules
        if isinstance(rule, ExactJoinRule) and not rule.map_output
    }

    def input_datasets(rule: ExactJoinRule) -> tuple[SourceDataset, ...]:
        result: list[SourceDataset] = []

        def add(dataset_id: str) -> None:
            hidden = hidden_by_output.get(dataset_id)
            if hidden is None:
                dataset = datasets[dataset_id]
                if dataset.dataset_id not in {
                    item.dataset_id for item in result
                }:
                    result.append(dataset)
                return
            add(hidden.left_dataset_id)
            add(hidden.right_dataset_id)

        add(rule.left_dataset_id)
        add(rule.right_dataset_id)
        return tuple(result)

    result: list[SavedExactJoinView] = []
    structural_step = 0
    for rule in plan.rules:
        if not isinstance(rule, (ExactJoinRule, UnionAllRule, GroupAggregateRule)):
            continue
        structural_step += 1
        if not isinstance(rule, ExactJoinRule) or not rule.map_output:
            continue
        inputs = input_datasets(rule)
        grouped = rule.left_dataset_id in hidden_by_output or rule.right_dataset_id in hidden_by_output
        left = inputs[0] if grouped else datasets[rule.left_dataset_id]
        right = inputs[-1] if grouped else datasets[rule.right_dataset_id]
        output = datasets[structural_dataset_id(rule)]
        sources = saved_join_field_sources(plan, rule)
        result.append(
            {
                "rule": rule,
                "review": reviews.get(rule.rule_id),
                "left": left,
                "right": right,
                "output": output,
                "left_use": (
                    uses[left.dataset_id] if left.dataset_id in physical_ids else None
                ),
                "right_use": (
                    uses[right.dataset_id] if right.dataset_id in physical_ids else None
                ),
                "step": structural_step,
                "inputs": inputs,
                "grouped": grouped,
                "columns": tuple(
                    (
                        datasets[sources[column.column_key][0]].name,
                        next(item.source_name for item in datasets[sources[column.column_key][0]].columns if item.stable_key == sources[column.column_key][1]),
                        column.source_name,
                    )
                    for column in rule.output_columns
                ),
            }
        )
    return tuple(result)


def source_use_label(use: SourceDatasetUse | None) -> str:
    """Return the exact data-manager label for one physical-use policy."""

    return {
        SourceDatasetUse.MAP_DIRECTLY: "Map directly",
        SourceDatasetUse.PREPARATION_ONLY: "Preparation only",
        SourceDatasetUse.MAP_AND_PREPARE: "Map and prepare",
        None: "Prepared logical dataset",
    }[use]
