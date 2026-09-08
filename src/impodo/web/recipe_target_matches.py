"""Build and apply focused target-value reviews for Recipe applications.

The review is intentionally narrower than the mapping editor.  It exposes
only bounded categorical source values and the target choices already captured
during the run's Odoo check.  Recipe definitions remain immutable; decisions
are written only to the current Recipe application mapping.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from impodo.domain.mapping.contracts import (
    CategoricalCoveragePolicy,
    DatasetMapping,
    MappingDefinition,
    RelationshipValueSource,
    ResolverOrigin,
    ScalarValueSource,
    ValueMapping,
)
from impodo.domain.mapping.scalar_values import (
    ScalarValueError,
    evaluate_scalar_mapping_value,
)
from impodo.domain.workspace.errors import WorkspaceError
from impodo.domain.workspace.reference_keys import standard_reference_key
from impodo.domain.workspace.supporting_lookups import SupportingLookupSnapshot

from .context import WebContext


@dataclass(frozen=True, slots=True)
class TargetValueChoice:
    """One portable target choice shown in a focused selector."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class TargetValueRow:
    """One distinct source value and its current target-bound decision."""

    source_value: str
    row_count: int
    current_target_value: str
    current_target_label: str
    needs_review: bool
    input_name: str
    selected_target_value: str


@dataclass(frozen=True, slots=True)
class TargetMatchField:
    """One Selection or Many2one mapping represented as a compact task."""

    dataset_index: int
    mapping_index: int
    mapping_kind: str
    dataset_name: str
    target_model: str
    target_model_label: str
    target_field: str
    target_field_label: str
    source_column_label: str
    kind_label: str
    choices: tuple[TargetValueChoice, ...]
    rows: tuple[TargetValueRow, ...]
    blocking_reason: str = ""

    @property
    def needs_review_count(self) -> int:
        return sum(item.needs_review for item in self.rows)

    @property
    def verified_rows(self) -> tuple[TargetValueRow, ...]:
        return tuple(item for item in self.rows if not item.needs_review)

    @property
    def review_rows(self) -> tuple[TargetValueRow, ...]:
        return tuple(item for item in self.rows if item.needs_review)


@dataclass(frozen=True, slots=True)
class TargetMatchReview:
    """Focused, target-bound review state for one Recipe application."""

    fields: tuple[TargetMatchField, ...]
    working_draft_version: int
    parent_mapping_version: int | None
    definition_hash: str

    @property
    def needs_review_count(self) -> int:
        return sum(item.needs_review_count for item in self.fields)

    @property
    def verified_count(self) -> int:
        return sum(len(item.verified_rows) for item in self.fields)

    @property
    def can_confirm(self) -> bool:
        return bool(self.fields) and not any(
            item.blocking_reason for item in self.fields
        )


def build_target_match_review(
    context: WebContext,
    application,
    *,
    submitted: Mapping[str, str] | None = None,
) -> TargetMatchReview:
    """Recompute current source coverage and reuse captured Odoo choices."""

    workspace_id = application.workspace_id
    working = context.queries.get_mapping_working_draft(workspace_id)
    selection = context.queries.get_mapping_source_selection(workspace_id)
    schema = context.queries.get_odoo_schema_catalog(workspace_id)
    if working is None or selection is None or schema is None:
        raise WorkspaceError(
            "Recheck Odoo before reviewing target-specific values"
        )
    bundle = context.run_planning.repository.get_bundle(
        application.migration_run_id
    )
    setup_workspace = next(
        (item for item in bundle.workspaces if item.recipe_application_id is None),
        None,
    )
    if setup_workspace is None:
        raise WorkspaceError("The run's Odoo check workspace is unavailable")
    setup_schema = context.queries.get_odoo_schema_catalog(
        setup_workspace.workspace_id
    )
    if setup_schema is None:
        raise WorkspaceError("Recheck Odoo before reviewing linked values")
    coverage = context.categorical_coverage.collect(
        workspace_id,
        working.definition,
        selection,
        schema,
    )
    coverage_by_path = {
        item.path: item for item in coverage.evidence.field_results
    }
    source_datasets = {item.dataset_id: item for item in selection.datasets}
    schema_models = {item.name: item for item in schema.models}
    fields: list[TargetMatchField] = []
    lookup_cache: dict[
        tuple[str, tuple[str, ...], tuple[str, ...]],
        SupportingLookupSnapshot | None,
    ] = {}

    for dataset_index, dataset in enumerate(working.definition.datasets):
        source_dataset = source_datasets.get(dataset.dataset_id)
        source_columns = {
            item.stable_key: item.source_name
            for item in (source_dataset.columns if source_dataset else ())
        }
        target_model = schema_models.get(dataset.target_model)
        target_fields = {
            item.name: item for item in (target_model.fields if target_model else ())
        }
        for mapping_index, scalar in enumerate(dataset.fields):
            path = f"/datasets/{dataset_index}/fields/{mapping_index}"
            result = coverage_by_path.get(path)
            metadata = target_fields.get(scalar.target_field)
            if (
                result is None
                or metadata is None
                or metadata.type != "selection"
                or scalar.source_column_key is None
                or scalar.value_source
                not in {
                    ScalarValueSource.SOURCE,
                    ScalarValueSource.SOURCE_WITH_FALLBACK,
                }
                or len(result.source_column_keys) != 1
            ):
                continue
            choices = tuple(
                TargetValueChoice(str(value), str(label))
                for value, label in metadata.selection
            )
            rows = _target_value_rows(
                dataset_index,
                mapping_index,
                "scalar",
                result,
                choices,
                dict(
                    (item.source_value, item.target_value)
                    for item in scalar.value_mappings
                ),
                exact_values=_scalar_exact_values(scalar, result),
                submitted=submitted,
            )
            fields.append(
                TargetMatchField(
                    dataset_index=dataset_index,
                    mapping_index=mapping_index,
                    mapping_kind="scalar",
                    dataset_name=(
                        source_dataset.name if source_dataset else dataset.dataset_id
                    ),
                    target_model=dataset.target_model,
                    target_model_label=(
                        target_model.label if target_model else dataset.target_model
                    ),
                    target_field=scalar.target_field,
                    target_field_label=metadata.label,
                    source_column_label=source_columns.get(
                        scalar.source_column_key,
                        scalar.source_column_key,
                    ),
                    kind_label="Selection",
                    choices=choices,
                    rows=rows,
                    blocking_reason=(
                        "This Odoo Selection field has no available choices."
                        if not choices and rows
                        else ""
                    ),
                )
            )

        for mapping_index, relationship in enumerate(dataset.relationships):
            path = f"/datasets/{dataset_index}/relationships/{mapping_index}"
            result = coverage_by_path.get(path)
            metadata = target_fields.get(relationship.target_field)
            resolver = relationship.resolver
            if (
                result is None
                or metadata is None
                or metadata.type != "many2one"
                or relationship.kind != "many2one"
                or relationship.value_source is not RelationshipValueSource.SOURCE
                or resolver.origin is not ResolverOrigin.TARGET_CATALOG
                or not resolver.model
            ):
                continue
            key_fields = tuple(
                item.target_field for item in resolver.key_mappings
            )
            scope_fields = tuple(
                item.target_field for item in resolver.scope_mappings
            )
            eligible = (
                len(result.source_column_keys) == 1
                and len(key_fields) == 1
                and not scope_fields
            )
            snapshot = None
            if eligible:
                cache_key = (resolver.model, key_fields, scope_fields)
                if cache_key not in lookup_cache:
                    lookup_cache[cache_key] = _current_supporting_lookup(
                        context,
                        setup_workspace.workspace_id,
                        setup_schema,
                        relation_model=resolver.model,
                        key_fields=key_fields,
                        scope_fields=scope_fields,
                    )
                snapshot = lookup_cache[cache_key]
            choices = tuple(
                TargetValueChoice(item.value, item.label)
                for item in (snapshot.choices if snapshot else ())
            )
            rows = _target_value_rows(
                dataset_index,
                mapping_index,
                "relationship",
                result,
                choices,
                dict(
                    (item.source_value, item.target_value)
                    for item in resolver.value_mappings
                ),
                exact_values={
                    item.values[0]: item.values[0]
                    for item in result.distinct_values
                    if len(item.values) == 1
                },
                ambiguous_values=(
                    frozenset(snapshot.ambiguous_values)
                    if snapshot is not None
                    else frozenset()
                ),
                submitted=submitted,
            )
            if not eligible:
                blocking_reason = (
                    "This linked field uses a composite or company-scoped key. "
                    "Open the full field matcher to review it."
                )
            elif snapshot is None:
                blocking_reason = (
                    "The captured Odoo choices are no longer current. "
                    "Recheck Odoo to refresh them."
                )
            elif not choices and rows:
                blocking_reason = "Odoo returned no choices for this linked field."
            else:
                blocking_reason = ""
            source_key = (
                result.source_column_keys[0]
                if len(result.source_column_keys) == 1
                else " + ".join(result.source_column_keys)
            )
            fields.append(
                TargetMatchField(
                    dataset_index=dataset_index,
                    mapping_index=mapping_index,
                    mapping_kind="relationship",
                    dataset_name=(
                        source_dataset.name if source_dataset else dataset.dataset_id
                    ),
                    target_model=dataset.target_model,
                    target_model_label=(
                        target_model.label if target_model else dataset.target_model
                    ),
                    target_field=relationship.target_field,
                    target_field_label=metadata.label,
                    source_column_label=source_columns.get(source_key, source_key),
                    kind_label="Linked record",
                    choices=choices,
                    rows=rows,
                    blocking_reason=blocking_reason,
                )
            )

    parent = context.mapping_workspace.mappings.get_mapping_revision(workspace_id)
    return TargetMatchReview(
        fields=tuple(fields),
        working_draft_version=working.version,
        parent_mapping_version=parent.version if parent else None,
        definition_hash=working.definition.content_hash,
    )


def apply_target_match_decisions(
    definition: MappingDefinition,
    review: TargetMatchReview,
    decisions: Mapping[str, str],
) -> tuple[DatasetMapping, ...]:
    """Patch only focused value-match policies in one application definition."""

    datasets = list(definition.datasets)
    for field in review.fields:
        if field.blocking_reason or not field.review_rows:
            continue
        additions: dict[str, str] = {}
        choice_values = {item.value for item in field.choices}
        for row in field.rows:
            if row.needs_review:
                target_value = decisions.get(row.input_name, "").strip()
                if target_value not in choice_values:
                    raise WorkspaceError(
                        f"Choose a current Odoo value for {row.source_value}"
                    )
                additions[row.source_value] = target_value
            elif row.current_target_value in choice_values:
                additions[row.source_value] = row.current_target_value
        dataset = datasets[field.dataset_index]
        if field.mapping_kind == "scalar":
            mappings = list(dataset.fields)
            current = mappings[field.mapping_index]
            merged = {
                item.source_value: item.target_value
                for item in current.value_mappings
            }
            merged.update(additions)
            mappings[field.mapping_index] = replace(
                current,
                value_mappings=tuple(
                    ValueMapping(source_value=source, target_value=target)
                    for source, target in sorted(merged.items())
                ),
                categorical_policy=(
                    CategoricalCoveragePolicy.EXPLICIT_VALUE_MATCH
                ),
            )
            datasets[field.dataset_index] = replace(
                dataset,
                fields=tuple(mappings),
            )
        else:
            relationships = list(dataset.relationships)
            current = relationships[field.mapping_index]
            merged = {
                item.source_value: item.target_value
                for item in current.resolver.value_mappings
            }
            merged.update(additions)
            relationships[field.mapping_index] = replace(
                current,
                resolver=replace(
                    current.resolver,
                    value_mappings=tuple(
                        ValueMapping(source_value=source, target_value=target)
                        for source, target in sorted(merged.items())
                    ),
                ),
                categorical_policy=(
                    CategoricalCoveragePolicy.EXPLICIT_KEY_MATCH
                ),
            )
            datasets[field.dataset_index] = replace(
                dataset,
                relationships=tuple(relationships),
            )
    return tuple(datasets)


def _target_value_rows(
    dataset_index: int,
    mapping_index: int,
    mapping_kind: str,
    result,
    choices: tuple[TargetValueChoice, ...],
    explicit_values: Mapping[str, str],
    *,
    exact_values: Mapping[str, str],
    ambiguous_values: frozenset[str] = frozenset(),
    submitted: Mapping[str, str] | None,
) -> tuple[TargetValueRow, ...]:
    choice_labels = {item.value: item.label for item in choices}
    choice_values = set(choice_labels).difference(ambiguous_values)
    uncovered = set(result.uncovered_values)
    rows: list[TargetValueRow] = []
    for row_index, item in enumerate(result.distinct_values):
        if len(item.values) != 1:
            continue
        source_value = item.values[0]
        current_target = explicit_values.get(
            source_value,
            exact_values.get(source_value, ""),
        )
        needs_review = (
            item.values in uncovered or current_target not in choice_values
        )
        input_name = (
            f"match_{dataset_index}_{mapping_kind}_{mapping_index}_{row_index}"
        )
        selected = (
            str(submitted.get(input_name, ""))
            if submitted is not None and needs_review
            else (
                current_target
                if current_target in choice_values
                else _suggested_choice(source_value, choices)
            )
        )
        rows.append(
            TargetValueRow(
                source_value=source_value,
                row_count=item.count,
                current_target_value=current_target,
                current_target_label=choice_labels.get(
                    current_target,
                    current_target,
                ),
                needs_review=needs_review,
                input_name=input_name,
                selected_target_value=selected,
            )
        )
    return tuple(rows)


def _scalar_exact_values(scalar, result) -> dict[str, str]:
    if scalar.categorical_policy is not CategoricalCoveragePolicy.EXACT_TARGET_VALUE:
        return {}
    exact: dict[str, str] = {}
    for item in result.distinct_values:
        if len(item.values) != 1:
            continue
        try:
            value = evaluate_scalar_mapping_value(
                scalar,
                item.values[0],
                source_values_by_key=dict(
                    zip(result.source_column_keys, item.values, strict=True)
                ),
            )
        except ScalarValueError:
            continue
        if value is not None:
            exact[item.values[0]] = str(value)
    return exact


def _suggested_choice(
    source_value: str,
    choices: tuple[TargetValueChoice, ...],
) -> str:
    normalized = source_value.strip().casefold()
    candidates = {
        item.value
        for item in choices
        if item.value.strip().casefold() == normalized
        or item.label.strip().casefold() == normalized
    }
    return next(iter(candidates)) if len(candidates) == 1 else ""


def _current_supporting_lookup(
    context: WebContext,
    setup_workspace_id: str,
    setup_schema,
    *,
    relation_model: str,
    key_fields: tuple[str, ...],
    scope_fields: tuple[str, ...],
) -> SupportingLookupSnapshot | None:
    """Read the target choices captured once by the run's Odoo check."""

    related_model = next(
        (item for item in setup_schema.models if item.name == relation_model),
        None,
    )
    available_fields = {
        item.name for item in (related_model.fields if related_model else ())
    }
    standard_key = standard_reference_key(relation_model)
    display_field = (
        standard_key.display_field
        if standard_key is not None
        and standard_key.key_fields == key_fields
        and standard_key.scope_fields == scope_fields
        else (
            "name"
            if "name" in available_fields
            else key_fields[0]
        )
    )
    return context.supporting_lookups.current(
        setup_workspace_id,
        relation_model=relation_model,
        key_fields=key_fields,
        scope_fields=scope_fields,
        display_field=display_field,
        target_hash=setup_schema.connection_target_hash,
        read_credential_binding_hash=(
            setup_schema.read_credential_binding_hash
        ),
        read_principal_hash=setup_schema.read_principal_hash,
        read_context_hash=setup_schema.read_context_hash,
        actor=context.actor,
    )
