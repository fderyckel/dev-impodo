"""Parse and present the bounded exact-join browser form.

Layer: web form adapter. Network values remain inert until this module resolves
them against the current frozen source selection and saved structural outputs.
The application service still owns the complete-data review and atomic save.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from starlette.datastructures import FormData

from impodo.application.workspace.derived_entities import ExactJoinRequest
from impodo.domain.structural import (
    MAX_STRUCTURAL_COLUMNS,
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
from impodo.domain.workspace.contracts import SourceDataset, SourceSelection
from impodo.domain.workspace.derived_entities import (
    DerivedEntityPlan,
    SourceDatasetUse,
    preparation_input_dataset_ids,
    validate_source_dataset_uses,
)

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
_JOIN_VALUE_TYPES = frozenset(
    {"string", "integer", "decimal", "boolean", "date", "datetime"}
)
_MAX_PROJECTION_CANDIDATES = 1_000


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

    raw_projection_indexes = tuple(form.getlist("projection"))
    if len(raw_projection_indexes) > MAX_STRUCTURAL_COLUMNS:
        raise ValueError(
            f"Choose no more than {MAX_STRUCTURAL_COLUMNS} output fields"
        )
    try:
        projection_indexes = tuple(int(value) for value in raw_projection_indexes)
    except (TypeError, ValueError) as error:
        raise ValueError("The selected output fields are invalid") from error
    if len(set(projection_indexes)) != len(projection_indexes) or any(
        index < 0 or index >= _MAX_PROJECTION_CANDIDATES
        for index in projection_indexes
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
            _text(form, "left_dataset_use")
            or SourceDatasetUse.PREPARATION_ONLY.value
        ),
        right_dataset_use=(
            _text(form, "right_dataset_use")
            or SourceDatasetUse.PREPARATION_ONLY.value
        ),
        projection_indexes=projection_indexes,
        projection_names=projection_names,
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
    return structural_mapping_selection(selection, structural_rules)


def exact_join_projection_candidates(
    selection: SourceSelection,
) -> tuple[tuple[SourceDataset, object], ...]:
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
    if plan and plan.dataset_uses:
        uses = validate_source_dataset_uses(selection, plan)
    else:
        prior_inputs = preparation_input_dataset_ids(plan.rules if plan else ())
        uses = {
            dataset_id: (
                SourceDatasetUse.MAP_AND_PREPARE
                if dataset_id in prior_inputs
                else SourceDatasetUse.MAP_DIRECTLY
            )
            for dataset_id in physical_ids
        }

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
) -> tuple[dict[str, object], ...]:
    """Describe saved exact joins without rereading their source rows."""

    if plan is None:
        return ()
    available = available_exact_join_selection(selection, plan)
    datasets = {dataset.dataset_id: dataset for dataset in available.datasets}
    reviews = {evidence.rule_id: evidence.review for evidence in plan.join_reviews}
    uses = validate_source_dataset_uses(selection, plan)
    physical_ids = {dataset.dataset_id for dataset in selection.datasets}
    result: list[dict[str, object]] = []
    structural_step = 0
    for rule in plan.rules:
        if not isinstance(rule, (ExactJoinRule, UnionAllRule, GroupAggregateRule)):
            continue
        structural_step += 1
        if not isinstance(rule, ExactJoinRule):
            continue
        left = datasets[rule.left_dataset_id]
        right = datasets[rule.right_dataset_id]
        output = datasets[structural_dataset_id(rule)]
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
