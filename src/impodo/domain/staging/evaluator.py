"""Extracted evaluator domain behavior."""

from __future__ import annotations

from dataclasses import (
    dataclass,
    replace,
)
from typing import (
    Callable,
    Iterable,
    Mapping,
)
import unicodedata

from ...derived_entities import (
    DerivedDatasetLink,
    DerivedEntityPlan,
    DerivedEntityRule,
    RelatedDatasetRule,
    _display_path,
    _normalized_path,
    derived_dataset_links,
)
from ..mapping.contracts import (
    DatasetMapping,
    IdentityComponentMapping,
    MappingDefinition,
    ScalarFieldMapping,
    ResolverOrigin,
    ScalarValueSource,
)
from ..coverage import ReferenceBundle, ReferenceDataSet, ReferenceEntry
from ..serialization import content_hash
from ...models import portable_value
from ..mapping.scalar_values import (
    ScalarValueError,
    ScalarValueRuleError,
    evaluate_scalar_mapping_value,
)
from ..mapping.descriptions import transformation_rule_summary
from ...models import (
    InvalidPreparedValue,
    Issue,
    Severity,
)
from ..compiler.contracts import CompiledMigrationPlan
from ...source import (
    PreparedBundle,
    SourceRow,
    SourceTable,
    prepare_source_tables,
)
from ...staging_contracts import (
    CanonicalStagingRun,
    StagingDatasetRole,
)
from ...workspace_contracts import (
    SourceDataset,
    SourceSelection,
)
from ..contracts import TRANSFORMATION_IMPACT_DETAIL_LIMIT
from ..errors import ReadinessError
from ..structural import (
    ExactJoinRule,
    GroupAggregateRule,
    StructuralError,
    UnionAllRule,
    execute_structural_rules,
)




from ..compiler.browser_mapping_compiler import compile_browser_mapping
from .control_totals import _evaluate_control_totals
from .fields import synthetic_field
from .scale import require_supported_browser_scale
from .transformation_impact import (
    TransformationImpactReport,
    _TransformationImpactCollector,
    _display_value,
    _display_values_equal,
    transformation_rule_impact_definitions,
)


@dataclass(frozen=True, slots=True)
class StagedBrowserMapping:
    """Complete in-memory output of Stage E mapping evaluation.

    The object connects the compiled mapping plan and prepared source bundle to
    the portable :class:`CanonicalStagingRun`. Display labels and physical-row
    coordinates support later quality/review screens; transformation impact is
    optional because it can be streamed into its own durable snapshot.
    """

    plan: CompiledMigrationPlan
    prepared: PreparedBundle
    canonical_run: CanonicalStagingRun
    dataset_labels: Mapping[str, str]
    source_field_labels: Mapping[tuple[str, str], str]
    physical_rows: Mapping[str, tuple[int, ...]]
    transformation_impact: TransformationImpactReport | None = None


@dataclass(frozen=True, slots=True)
class _IdentityImpactPlan:
    source_column_keys: tuple[str, ...]
    target_fields: tuple[str, ...]
    source_label: str


@dataclass(frozen=True, slots=True)
class _RelationshipValuePlan:
    source_column_key: str
    target_field: str
    target_by_source: Mapping[str, str]
    source_label: str
    rules: str


@dataclass(frozen=True, slots=True)
class _ScalarFieldPlan:
    index: int
    field: ScalarFieldMapping
    source_label: str
    rules: str


@dataclass(frozen=True, slots=True)
class _DatasetEvaluationPlan:
    dataset_id: str
    dataset_name: str
    ordinal_columns: tuple[tuple[int, str], ...]
    identities: tuple[_IdentityImpactPlan, ...]
    relationship_values: tuple[_RelationshipValuePlan, ...]
    scalar_fields: tuple[_ScalarFieldPlan, ...]


@dataclass(frozen=True, slots=True)
class _DerivedReferencePlan:
    rule: DerivedEntityRule
    source_column_key: str
    target_field: str


@dataclass(slots=True)
class ProjectedBrowserRow:
    """Row-local projection before any cross-row grouping decision."""

    number: int
    values: dict[str, object]
    parent_key: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class CompiledBrowserRowTransformer:
    """Compiled pure mapping behavior for one non-lookup effective dataset.

    Projection is separated from finishing so a caller can persist or resolve
    the parent de-duplication key before transformation impacts are emitted.
    """

    dataset_name: str
    effective_column_keys: tuple[str, ...]
    source_name_by_key: Mapping[str, str]
    evaluation_plan: _DatasetEvaluationPlan
    derived_reference_plan: tuple[_DerivedReferencePlan, ...]
    related_rule: RelatedDatasetRule | None
    role: str
    headers: tuple[str, ...]
    reference_indexes: Mapping[
        tuple[str, str],
        tuple[ReferenceDataSet, Mapping[str, ReferenceEntry]],
    ]

    def project(self, row: SourceRow) -> ProjectedBrowserRow:
        """Select stable columns and normalize governed related-row keys."""

        values = {
            stable_key: row.values.get(self.source_name_by_key[stable_key])
            for stable_key in self.effective_column_keys
        }
        rule = self.related_rule
        if rule is not None:
            for key in (
                rule.parent_key_column_key,
                rule.scope_column_key,
                rule.child_key_column_key if self.role == "child" else None,
            ):
                if key is not None and key in values:
                    values[key] = _normalized_key(values.get(key))

        parent_key: tuple[str, ...] | None = None
        if self.role == "parent" and rule is not None:
            keys = tuple(
                values.get(key)
                for key in (
                    rule.parent_key_column_key,
                    rule.scope_column_key,
                )
                if key is not None
            )
            if all(value is not None for value in keys):
                parent_key = tuple(str(value) for value in keys)
        return ProjectedBrowserRow(
            number=row.number,
            values=values,
            parent_key=parent_key,
        )

    def finish(
        self,
        projected: ProjectedBrowserRow,
        *,
        impact_collector: _TransformationImpactCollector | None = None,
    ) -> tuple[SourceRow, tuple[Issue, ...]]:
        """Apply the row-local mapping rules after cross-row admission."""

        issues = _normalize_derived_references(
            projected.values,
            self.derived_reference_plan,
            dataset=self.dataset_name,
            source_row=projected.number,
        )
        _record_identity_preparation(
            projected.values,
            self.evaluation_plan,
            source_row=projected.number,
            impact_collector=impact_collector,
        )
        _apply_relationship_value_mappings(
            projected.values,
            self.evaluation_plan,
            source_row=projected.number,
            impact_collector=impact_collector,
        )
        _apply_scalar_mappings(
            projected.values,
            self.evaluation_plan,
            source_row=projected.number,
            impact_collector=impact_collector,
            reference_indexes=self.reference_indexes,
        )
        return (
            SourceRow(number=projected.number, values=projected.values),
            issues,
        )


def evaluate_browser_mapping(
    *,
    project_id: str,
    definition: MappingDefinition,
    physical_selection: SourceSelection,
    effective_selection: SourceSelection,
    plan: DerivedEntityPlan | None,
    loaded_tables: Mapping[str, SourceTable],
    reference_bundle: ReferenceBundle | None = None,
    collect_transformation_impact: bool = False,
    transformation_detail_limit: int = TRANSFORMATION_IMPACT_DETAIL_LIMIT,
    transformation_impact_sink: Callable[[TransformationImpactRow], None]
    | None = None,
) -> StagedBrowserMapping:
    """Evaluate every frozen row without storage access or Odoo access.

    ``loaded_tables`` is keyed by physical dataset identifier.  The caller owns
    artifact materialization; this function owns mapping compilation,
    target-independent normalization, issue collection, lineage, and row
    reconciliation.
    """

    require_supported_browser_scale(physical_selection)
    if (
        physical_selection.project_id != project_id
        or effective_selection.project_id != project_id
        or (plan is not None and plan.project_id != project_id)
    ):
        raise ReadinessError("Canonical evaluation evidence belongs to another project")
    if reference_bundle is not None and reference_bundle.project_id != project_id:
        raise ReadinessError("Reference data belongs to another project")
    reference_indexes = _compile_reference_indexes(reference_bundle)
    if (
        plan is not None
        and plan.source_selection_hash != physical_selection.content_hash
    ):
        raise ReadinessError(
            "The related-record plan no longer matches its source data"
        )
    if definition.source_selection_hash != effective_selection.content_hash:
        raise ReadinessError("The submitted mapping no longer matches its source data")
    effective_by_id = {item.dataset_id: item for item in effective_selection.datasets}
    effective_by_name = {item.name: item for item in effective_selection.datasets}
    mapping_by_id = {item.dataset_id: item for item in definition.datasets}
    if len(effective_by_id) != len(effective_selection.datasets):
        raise ReadinessError("The frozen source contains duplicate dataset identifiers")
    if len(effective_by_name) != len(effective_selection.datasets):
        raise ReadinessError("The frozen source contains duplicate dataset names")
    if len(mapping_by_id) != len(definition.datasets):
        raise ReadinessError("The submitted mapping contains duplicate datasets")
    if set(mapping_by_id) != set(effective_by_id):
        raise ReadinessError("The submitted mapping does not cover every dataset")
    physical_by_id = {item.dataset_id: item for item in physical_selection.datasets}
    if len(physical_by_id) != len(physical_selection.datasets):
        raise ReadinessError(
            "The physical source contains duplicate dataset identifiers"
        )
    split_by_name = {
        name: (rule, role)
        for rule in (plan.rules if plan else ())
        if isinstance(rule, RelatedDatasetRule)
        for name, role in (
            (rule.parent_dataset_name, "parent"),
            (rule.child_dataset_name, "child"),
        )
    }
    lookup_links = derived_dataset_links(plan)
    lookup_rules = tuple(
        item
        for item in (plan.rules if plan else ())
        if isinstance(item, DerivedEntityRule)
    )
    lookup_by_dataset_id = {
        link.derived_dataset_id: (rule, link)
        for link, rule in zip(lookup_links, lookup_rules, strict=True)
    }
    impact_collector = (
        _TransformationImpactCollector(
            definition.content_hash,
            detail_limit=transformation_detail_limit,
            sink=transformation_impact_sink,
        )
        if collect_transformation_impact
        else None
    )
    if impact_collector is not None:
        for dataset_mapping in definition.datasets:
            for field in dataset_mapping.fields:
                for rule in transformation_rule_impact_definitions(
                    dataset_mapping.dataset_id, field
                ):
                    impact_collector.register_rule(rule)
    lookup_by_consumer: dict[
        str,
        list[tuple[DerivedEntityRule, DerivedDatasetLink]],
    ] = {}
    for link, rule in zip(lookup_links, lookup_rules, strict=True):
        lookup_by_consumer.setdefault(link.consumer_dataset_id, []).append(
            (rule, link)
        )

    if set(loaded_tables) != set(physical_by_id):
        raise ReadinessError("Loaded source tables do not match the frozen selection")
    for dataset_id, table in loaded_tables.items():
        physical = physical_by_id[dataset_id]
        if table.dataset != physical.name:
            raise ReadinessError("A loaded source table has the wrong dataset name")
        expected_hash = physical.source_sha256.removeprefix("sha256:")
        if table.content_hash != f"sha256:{expected_hash}":
            raise ReadinessError("Stored source content changed after selection")

    structural_rules = tuple(
        item
        for item in (plan.rules if plan else ())
        if isinstance(item, (ExactJoinRule, UnionAllRule, GroupAggregateRule))
    )
    try:
        structural_execution = execute_structural_rules(
            selection=physical_selection,
            loaded_tables=loaded_tables,
            rules=structural_rules,
        )
    except StructuralError as error:
        raise ReadinessError(str(error)) from error
    structural_by_id = structural_execution.by_dataset_id()

    compiled_plan = compile_browser_mapping(
        definition,
        effective_selection,
        derived_plan_hash=plan.content_hash if plan is not None else None,
    )
    staged_tables: list[SourceTable] = []
    preparation_issues: list[Issue] = []
    source_labels: dict[tuple[str, str], str] = {}
    source_lineage: dict[
        tuple[str, int],
        tuple[str, tuple[int, ...]] | Mapping[str, tuple[int, ...]],
    ] = {}
    dataset_evidence: dict[
        str, tuple[str, StagingDatasetRole, int]
    ] = {}
    for dataset_spec in compiled_plan.datasets:
        effective = effective_by_name[dataset_spec.name]
        mapping = mapping_by_id[effective.dataset_id]
        structural = structural_by_id.get(effective.dataset_id)
        lookup = lookup_by_dataset_id.get(effective.dataset_id)
        split = split_by_name.get(effective.name)
        if structural is not None:
            physical = effective
            role = {
                ExactJoinRule: "join",
                UnionAllRule: "union",
                GroupAggregateRule: "group",
            }[type(next(item for item in structural_rules if item.output_dataset_name == effective.name))]
            rule = None
        elif lookup is not None:
            lookup_rule, lookup_link = lookup
            physical = physical_by_id.get(lookup_rule.source_dataset_id)
            role = "lookup"
            rule = None
        elif split is None:
            physical = physical_by_id.get(effective.dataset_id)
            role = "source"
            rule = None
        else:
            rule, role = split
            physical = physical_by_id.get(rule.source_dataset_id)
        if physical is None:
            raise ReadinessError("Prepared dataset no longer has a source")
        if structural is not None:
            staged, issues, _ = _stage_table(
                effective,
                physical,
                mapping,
                structural.table,
                None,
                "source",
                (),
                impact_collector=impact_collector,
                reference_indexes=reference_indexes,
            )
            row_lineage = structural.lineage
        elif lookup is not None:
            staged, issues, row_lineage = _stage_derived_table(
                effective,
                physical,
                mapping,
                loaded_tables[physical.dataset_id],
                lookup_rule,
                lookup_link,
                impact_collector=impact_collector,
                reference_indexes=reference_indexes,
            )
        else:
            staged, issues, row_lineage = _stage_table(
                effective,
                physical,
                mapping,
                loaded_tables[physical.dataset_id],
                rule,
                role,
                tuple(lookup_by_consumer.get(effective.dataset_id, ())),
                impact_collector=impact_collector,
                reference_indexes=reference_indexes,
            )
        staged_tables.append(staged)
        preparation_issues.extend(issues)
        if structural is not None:
            source_lineage.update(
                {
                    (effective.name, source_row): physical_sources
                    for source_row, physical_sources in row_lineage.items()
                }
            )
        else:
            source_lineage.update(
                {
                    (effective.name, source_row): (
                        physical.dataset_id,
                        physical_rows,
                    )
                    for source_row, physical_rows in row_lineage.items()
                }
            )
        dataset_evidence[effective.name] = (
            physical.dataset_id,
            {
                "source": StagingDatasetRole.DIRECT,
                "parent": StagingDatasetRole.PARENT,
                "child": StagingDatasetRole.CHILD,
                "lookup": StagingDatasetRole.LOOKUP,
                "join": StagingDatasetRole.JOIN,
                "union": StagingDatasetRole.UNION,
                "group": StagingDatasetRole.GROUP,
            }[role],
            (
                structural.reconciliation.input_rows
                if structural is not None
                else len(loaded_tables[physical.dataset_id].rows)
            ),
        )
        for column in effective.columns:
            source_labels[(effective.name, column.stable_key)] = column.source_name
        column_name_by_key = {
            column.stable_key: column.source_name for column in effective.columns
        }
        for index, field in enumerate(mapping.fields):
            if field.value_source is ScalarValueSource.ODOO_DEFAULT:
                continue
            source_labels[(effective.name, synthetic_field(index))] = (
                column_name_by_key.get(field.source_column_key or "")
                or field.target_field
            )

    prepared = prepare_source_tables(
        compiled_plan,
        staged_tables,
        source_hashes={
            item.name: f"sha256:{item.source_sha256.removeprefix('sha256:')}"
            for item in effective_selection.datasets
        },
    )
    if preparation_issues:
        prepared = _attach_preparation_issues(
            prepared,
            preparation_issues,
        )
    canonical_run = CanonicalStagingRun.from_prepared(
        project_id=project_id,
        mapping_id=definition.mapping_id,
        physical_selection_hash=physical_selection.content_hash,
        source_selection_hash=effective_selection.content_hash,
        mapping_hash=definition.content_hash,
        schema_hash=definition.schema_hash,
        derived_plan_hash=plan.content_hash if plan is not None else None,
        plan=compiled_plan,
        prepared=prepared,
        field_sources=_canonical_field_sources(definition, effective_selection),
        source_lineage=source_lineage,
        dataset_evidence=dataset_evidence,
        control_totals=_evaluate_control_totals(
            definition,
            effective_selection,
            prepared,
        ),
    )
    return StagedBrowserMapping(
        plan=compiled_plan,
        prepared=prepared,
        canonical_run=canonical_run,
        dataset_labels={
            item.name: item.name.replace("_", " ").title()
            for item in effective_selection.datasets
        },
        source_field_labels=source_labels,
        physical_rows={
            dataset_id: tuple(row.number for row in table.rows)
            for dataset_id, table in sorted(loaded_tables.items())
        },
        transformation_impact=(
            impact_collector.report() if impact_collector is not None else None
        ),
    )


def _canonical_field_sources(
    definition: MappingDefinition,
    selection: SourceSelection,
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Describe which source columns govern each proposed target value."""

    dataset_by_id = {item.dataset_id: item for item in selection.datasets}
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for mapping in definition.datasets:
        dataset = dataset_by_id[mapping.dataset_id]
        fields: dict[str, tuple[str, ...]] = {
            "$source_identity": mapping.source_identity_column_keys,
        }
        for component in (*mapping.target_identity, *mapping.target_scope):
            for target_field in component.target_fields:
                fields[target_field] = component.source_column_keys
        for field in mapping.fields:
            if field.value_source is ScalarValueSource.ODOO_DEFAULT:
                continue
            fields[field.target_field] = (
                (field.source_column_key,) if field.source_column_key else ()
            )
        for relationship in mapping.relationships:
            fields[relationship.target_field] = relationship.source_column_keys
        result[dataset.name] = dict(sorted(fields.items()))
    return result


def canonical_field_sources(
    definition: MappingDefinition,
    selection: SourceSelection,
) -> dict[str, dict[str, tuple[str, ...]]]:
    """Expose the one canonical source-field lineage compiler to streaming."""

    return _canonical_field_sources(definition, selection)


def _compile_dataset_evaluation_plan(
    effective: SourceDataset,
    mapping: DatasetMapping,
) -> _DatasetEvaluationPlan:
    """Compile row-invariant mapping work once for one dataset."""

    labels = {
        column.stable_key: column.source_name for column in effective.columns
    }
    identities = tuple(
        _compile_identity_impact(component, labels)
        for component in (*mapping.target_identity, *mapping.target_scope)
    )
    relationship_values = tuple(
        _RelationshipValuePlan(
            source_column_key=relationship.source_column_keys[0],
            target_field=relationship.target_field,
            target_by_source={
                item.source_value: item.target_value
                for item in relationship.resolver.value_mappings
            },
            source_label=labels.get(
                relationship.source_column_keys[0],
                "Matched value",
            ),
            rules=(
                "Reviewed value match "
                f"({len(relationship.resolver.value_mappings)} confirmed choice(s))"
            ),
        )
        for relationship in mapping.relationships
        if relationship.resolver.value_mappings
        and len(relationship.source_column_keys) == 1
    )
    scalar_fields = tuple(
        _ScalarFieldPlan(
            index=index,
            field=field,
            source_label=(
                labels.get(field.source_column_key or "") or "Constant value"
            ),
            rules=transformation_rule_summary(field),
        )
        for index, field in enumerate(mapping.fields)
        if field.value_source is not ScalarValueSource.ODOO_DEFAULT
    )
    return _DatasetEvaluationPlan(
        dataset_id=mapping.dataset_id,
        dataset_name=effective.name,
        ordinal_columns=tuple(
            (column.ordinal, column.stable_key) for column in effective.columns
        ),
        identities=identities,
        relationship_values=relationship_values,
        scalar_fields=scalar_fields,
    )


def _compile_identity_impact(
    component: IdentityComponentMapping,
    labels: Mapping[str, str],
) -> _IdentityImpactPlan:
    return _IdentityImpactPlan(
        source_column_keys=component.source_column_keys,
        target_fields=component.target_fields,
        source_label=" + ".join(
            labels.get(key, "Identity field")
            for key in component.source_column_keys
        ),
    )


def compile_browser_row_transformer(
    effective: SourceDataset,
    physical: SourceDataset,
    mapping: DatasetMapping,
    rule: RelatedDatasetRule | None,
    role: str,
    lookup_bindings: tuple[
        tuple[DerivedEntityRule, DerivedDatasetLink], ...
    ] = (),
    reference_indexes: Mapping[
        tuple[str, str],
        tuple[ReferenceDataSet, Mapping[str, ReferenceEntry]],
    ] | None = None,
) -> CompiledBrowserRowTransformer:
    """Compile the row-local half of browser staging exactly once."""

    headers = (
        *(column.stable_key for column in effective.columns),
        *(
            synthetic_field(index)
            for index, field in enumerate(mapping.fields)
            if field.value_source is not ScalarValueSource.ODOO_DEFAULT
        ),
    )
    return CompiledBrowserRowTransformer(
        dataset_name=effective.name,
        effective_column_keys=tuple(
            column.stable_key for column in effective.columns
        ),
        source_name_by_key={
            column.stable_key: column.source_name for column in physical.columns
        },
        evaluation_plan=_compile_dataset_evaluation_plan(effective, mapping),
        derived_reference_plan=_compile_derived_reference_plan(
            mapping,
            lookup_bindings,
        ),
        related_rule=rule,
        role=role,
        headers=tuple(headers),
        reference_indexes=reference_indexes or {},
    )


def _stage_table(
    effective: SourceDataset,
    physical: SourceDataset,
    mapping: DatasetMapping,
    table: SourceTable,
    rule: RelatedDatasetRule | None,
    role: str,
    lookup_bindings: tuple[
        tuple[DerivedEntityRule, DerivedDatasetLink], ...
    ] = (),
    *,
    impact_collector: _TransformationImpactCollector | None = None,
    reference_indexes: Mapping[
        tuple[str, str],
        tuple[ReferenceDataSet, Mapping[str, ReferenceEntry]],
    ] | None = None,
) -> tuple[
    SourceTable,
    tuple[Issue, ...],
    dict[int, tuple[int, ...]],
]:
    transformer = compile_browser_row_transformer(
        effective,
        physical,
        mapping,
        rule,
        role,
        lookup_bindings,
        reference_indexes,
    )
    staged_rows: list[SourceRow] = []
    issues: list[Issue] = []
    parent_row_by_key: dict[tuple[str, ...], int] = {}
    source_rows_by_output: dict[int, list[int]] = {}
    for row in table.rows:
        projected = transformer.project(row)
        if projected.parent_key is not None:
            existing_row = parent_row_by_key.get(projected.parent_key)
            if existing_row is not None:
                source_rows_by_output[existing_row].append(row.number)
                continue
            parent_row_by_key[projected.parent_key] = row.number
        staged_row, row_issues = transformer.finish(
            projected,
            impact_collector=impact_collector,
        )
        issues.extend(row_issues)
        staged_rows.append(staged_row)
        source_rows_by_output[row.number] = [row.number]
    return (
        SourceTable(
            dataset=effective.name,
            path=table.path,
            headers=transformer.headers,
            rows=tuple(staged_rows),
            content_hash=table.content_hash,
        ),
        tuple(issues),
        {
            output_row: tuple(source_rows)
            for output_row, source_rows in source_rows_by_output.items()
        },
    )


def _stage_derived_table(
    effective: SourceDataset,
    physical: SourceDataset,
    mapping: DatasetMapping,
    table: SourceTable,
    rule: DerivedEntityRule,
    link: DerivedDatasetLink,
    *,
    impact_collector: _TransformationImpactCollector | None = None,
    reference_indexes: Mapping[
        tuple[str, str],
        tuple[ReferenceDataSet, Mapping[str, ReferenceEntry]],
    ] | None = None,
) -> tuple[
    SourceTable,
    tuple[Issue, ...],
    dict[int, tuple[int, ...]],
]:
    """Materialize every unique related record from the full source table."""

    evaluation_plan = _compile_dataset_evaluation_plan(effective, mapping)
    source_column = next(
        item
        for item in physical.columns
        if item.stable_key == rule.source_column_key
    )
    accumulated: dict[tuple[str, ...], dict[str, object]] = {}
    for row in table.rows:
        path = _normalized_path(
            row.values.get(source_column.source_name),
            rule.parent_separator,
        )
        if path is None:
            continue
        display_parts, key_parts = path
        if not display_parts:
            continue
        for depth in range(1, len(key_parts) + 1):
            key_path = key_parts[:depth]
            display_path = display_parts[:depth]
            entry = accumulated.setdefault(
                key_path,
                {
                    "name": display_path[-1],
                    "aliases": set(),
                    "source_row": row.number,
                    "source_rows": set(),
                },
            )
            aliases = entry["aliases"]
            assert isinstance(aliases, set)
            aliases.add(_display_path(display_path, rule.parent_separator))
            source_rows = entry["source_rows"]
            assert isinstance(source_rows, set)
            source_rows.add(row.number)

    rows: list[SourceRow] = []
    issues: list[Issue] = []
    source_rows_by_output: dict[int, tuple[int, ...]] = {}
    ordered_candidates = sorted(
        accumulated.items(),
        key=lambda item: (len(item[0]), item[0]),
    )
    for generated_row, (key_path, entry) in enumerate(
        ordered_candidates,
        start=2,
    ):
        values: dict[str, object] = {
            link.canonical_key_column_key: " / ".join(key_path),
            link.name_column_key: str(entry["name"]),
        }
        if link.parent_key_column_key is not None:
            values[link.parent_key_column_key] = (
                " / ".join(key_path[:-1]) if key_path[:-1] else None
            )
        _record_identity_preparation(
            values,
            evaluation_plan,
            source_row=generated_row,
            impact_collector=impact_collector,
        )
        _apply_relationship_value_mappings(
            values,
            evaluation_plan,
            source_row=generated_row,
            impact_collector=impact_collector,
        )
        _apply_scalar_mappings(
            values,
            evaluation_plan,
            source_row=generated_row,
            impact_collector=impact_collector,
            reference_indexes=reference_indexes or {},
        )
        evidence_row = int(entry["source_row"])
        aliases = entry["aliases"]
        assert isinstance(aliases, set)
        if len(aliases) > 1:
            issues.append(
                Issue(
                    code="DERIVED_ALIAS_REVIEW_REQUIRED",
                    message=(
                        "multiple source spellings produce the same related "
                        "record; review the preferred display value "
                        f"(first seen at source row {evidence_row})"
                    ),
                    severity=Severity.ERROR,
                    dataset=effective.name,
                    row=generated_row,
                    field=link.name_column_key,
                )
            )
        rows.append(SourceRow(number=generated_row, values=values))
        source_rows = entry["source_rows"]
        assert isinstance(source_rows, set)
        source_rows_by_output[generated_row] = tuple(sorted(source_rows))

    headers = (
        *(column.stable_key for column in effective.columns),
        *(
            synthetic_field(index)
            for index, field in enumerate(mapping.fields)
            if field.value_source is not ScalarValueSource.ODOO_DEFAULT
        ),
    )
    return (
        SourceTable(
            dataset=effective.name,
            path=table.path,
            headers=tuple(headers),
            rows=tuple(rows),
            content_hash=table.content_hash,
        ),
        tuple(issues),
        source_rows_by_output,
    )


def _compile_derived_reference_plan(
    mapping: DatasetMapping,
    lookup_bindings: tuple[
        tuple[DerivedEntityRule, DerivedDatasetLink], ...
    ],
) -> tuple[_DerivedReferencePlan, ...]:
    target_by_derived_source: dict[tuple[str, str], str] = {}
    for relationship in mapping.relationships:
        if relationship.resolver.origin is not ResolverOrigin.DATASET:
            continue
        derived_dataset_id = relationship.resolver.dataset_id
        if derived_dataset_id is None:
            continue
        for source_column_key in relationship.source_column_keys:
            target_by_derived_source.setdefault(
                (derived_dataset_id, source_column_key),
                relationship.target_field,
            )
    return tuple(
        _DerivedReferencePlan(
            rule=rule,
            source_column_key=link.source_column_key,
            target_field=target_field,
        )
        for rule, link in lookup_bindings
        for target_field in (
            target_by_derived_source.get(
                (link.derived_dataset_id, link.source_column_key)
            ),
        )
        if target_field is not None
    )


def _normalize_derived_references(
    values: dict[str, object],
    plan: tuple[_DerivedReferencePlan, ...],
    *,
    dataset: str,
    source_row: int,
) -> tuple[Issue, ...]:
    issues: list[Issue] = []
    for item in plan:
        path = _normalized_path(
            values.get(item.source_column_key),
            item.rule.parent_separator,
        )
        if path is not None and path[0]:
            values[item.source_column_key] = " / ".join(path[1])
            continue
        values[item.source_column_key] = None
        issues.append(
            Issue(
                code=(
                    "DERIVED_REFERENCE_QUARANTINED"
                    if item.rule.blank_policy == "quarantine"
                    else "DERIVED_REFERENCE_MISSING"
                ),
                message=(
                    "the source value cannot identify a related record in "
                    f"{item.rule.output_dataset_name}"
                ),
                severity=Severity.ERROR,
                dataset=dataset,
                row=source_row,
                field=item.target_field,
            )
        )
    return tuple(issues)


def _apply_scalar_mappings(
    values: dict[str, object],
    plan: _DatasetEvaluationPlan,
    *,
    source_row: int,
    impact_collector: _TransformationImpactCollector | None = None,
    reference_indexes: Mapping[
        tuple[str, str],
        tuple[ReferenceDataSet, Mapping[str, ReferenceEntry]],
    ] | None = None,
) -> None:
    source_values_by_ordinal = {
        ordinal: values.get(stable_key)
        for ordinal, stable_key in plan.ordinal_columns
    }
    for field_plan in plan.scalar_fields:
        field = field_plan.field
        raw = (
            values.get(field.source_column_key)
            if field.source_column_key is not None
            else None
        )
        try:
            scalar_input = raw
            if field.reference_lookup is not None:
                raw = tuple(
                    values.get(key)
                    for key in field.reference_lookup.key_source_column_keys
                )
                scalar_input = _reference_lookup_value(
                    field,
                    values,
                    reference_indexes or {},
                )
            rules = transformation_rule_impact_definitions(
                plan.dataset_id, field
            )
            rules_by_step = {
                step_index: rule
                for (step_index, step), rule in zip(
                    (
                        (step_index, step)
                        for step_index, step in enumerate(
                            field.transform.effective_text_steps
                        )
                        if step.configured
                    ),
                    rules,
                    strict=True,
                )
            }
            proposed = evaluate_scalar_mapping_value(
                field,
                scalar_input,
                source_values_by_ordinal=source_values_by_ordinal,
                text_step_observer=(
                    (
                        lambda step_index, matched, changed, configured=rules_by_step: (
                            impact_collector.record_rule(
                                configured[step_index],
                                matched=matched,
                                changed=changed,
                            )
                        )
                    )
                    if impact_collector is not None and rules_by_step
                    else None
                ),
            )
            values[synthetic_field(field_plan.index)] = proposed
            if impact_collector is not None:
                outcome = _transformation_outcome(field, raw, proposed)
                impact_collector.record(
                    dataset=plan.dataset_name,
                    source_row=source_row,
                    source_column=field_plan.source_label,
                    target_field=field.target_field,
                    raw_value=raw,
                    proposed_value=proposed,
                    rules=field_plan.rules,
                    outcome=outcome,
                )
        except ScalarValueRuleError as error:
            values[synthetic_field(field_plan.index)] = InvalidPreparedValue(
                code=error.code,
                message=str(error),
            )
            if impact_collector is not None:
                impact_collector.record(
                    dataset=plan.dataset_name,
                    source_row=source_row,
                    source_column=field_plan.source_label,
                    target_field=field.target_field,
                    raw_value=raw,
                    proposed_value="Invalid",
                    rules=field_plan.rules,
                    outcome="invalid",
                    message=str(error),
                )
        except ScalarValueError as error:
            values[synthetic_field(field_plan.index)] = (
                None
                if "required value" in str(error).casefold()
                else "__impodo_invalid_value__"
            )
            if impact_collector is not None:
                impact_collector.record(
                    dataset=plan.dataset_name,
                    source_row=source_row,
                    source_column=field_plan.source_label,
                    target_field=field.target_field,
                    raw_value=raw,
                    proposed_value="Invalid",
                    rules=field_plan.rules,
                    outcome="invalid",
                    message=str(error),
                )


def _compile_reference_indexes(
    bundle: ReferenceBundle | None,
) -> dict[
    tuple[str, str],
    tuple[ReferenceDataSet, dict[str, ReferenceEntry]],
]:
    """Compile exact typed reference keys once for bounded row evaluation."""

    if bundle is None:
        return {}
    return {
        (dataset.reference_id, dataset.content_hash): (
            dataset,
            {entry.key_hash: entry for entry in dataset.entries},
        )
        for dataset in bundle.datasets
    }


def compile_reference_indexes(
    bundle: ReferenceBundle | None,
) -> dict[
    tuple[str, str],
    tuple[ReferenceDataSet, dict[str, ReferenceEntry]],
]:
    """Expose the compiled exact-key index to bounded row evaluation."""

    return _compile_reference_indexes(bundle)


def _reference_lookup_value(
    field: ScalarFieldMapping,
    values: Mapping[str, object],
    indexes: Mapping[
        tuple[str, str],
        tuple[ReferenceDataSet, Mapping[str, ReferenceEntry]],
    ],
) -> object:
    lookup = field.reference_lookup
    if lookup is None:
        raise ScalarValueRuleError(
            "REFERENCE_POLICY_MISSING",
            "The reference lookup policy is missing.",
        )
    indexed = indexes.get((lookup.reference_id, lookup.reference_content_hash))
    if indexed is None:
        raise ScalarValueRuleError(
            "REFERENCE_INPUT_MISSING",
            "The approved reference list is missing or has changed.",
        )
    dataset, entries = indexed
    if lookup.value_field not in dataset.value_kinds:
        raise ScalarValueRuleError(
            "REFERENCE_OUTPUT_MISSING",
            "The approved reference list no longer provides this output.",
        )
    key = tuple(values.get(item) for item in lookup.key_source_column_keys)
    if any(
        item is None or (isinstance(item, str) and not item.strip())
        for item in key
    ):
        if lookup.on_blank == "null":
            return None
        raise ScalarValueRuleError(
            "REFERENCE_KEY_BLANK",
            "A required reference key is blank.",
        )
    entry = entries.get(content_hash(portable_value(key)))
    if entry is None:
        if lookup.on_unknown == "null":
            return None
        raise ScalarValueRuleError(
            "REFERENCE_KEY_UNKNOWN",
            "The source value is not present in the approved reference list.",
        )
    return entry.values[lookup.value_field]


def _record_identity_preparation(
    values: Mapping[str, object],
    plan: _DatasetEvaluationPlan,
    *,
    source_row: int,
    impact_collector: _TransformationImpactCollector | None,
) -> None:
    """Expose identity whitespace cleanup as an explicit reviewable change."""

    if impact_collector is None:
        return
    for identity in plan.identities:
        raw_values = tuple(values.get(key) for key in identity.source_column_keys)
        proposed_values = tuple(
            (
                " ".join(str(value).strip().split())
                if value is not None and " ".join(str(value).strip().split())
                else None
            )
            for value in raw_values
        )
        if all(
            _display_values_equal(raw, proposed)
            for raw, proposed in zip(raw_values, proposed_values, strict=True)
        ):
            continue
        raw_display = " | ".join(_display_value(item) for item in raw_values)
        proposed_display = " | ".join(
            _display_value(item) for item in proposed_values
        )
        for target_field in identity.target_fields:
            impact_collector.record(
                dataset=plan.dataset_name,
                source_row=source_row,
                source_column=identity.source_label,
                target_field=target_field,
                raw_value=raw_display,
                proposed_value=proposed_display,
                rules="Identity preparation",
                outcome="changed",
            )


def _apply_relationship_value_mappings(
    values: dict[str, object],
    plan: _DatasetEvaluationPlan,
    *,
    source_row: int,
    impact_collector: _TransformationImpactCollector | None = None,
) -> None:
    """Replace authored source choices with confirmed Odoo business keys."""

    for relationship in plan.relationship_values:
        raw_value = values.get(relationship.source_column_key)
        if raw_value is None:
            continue
        source_value = str(raw_value).strip()
        target_value = relationship.target_by_source.get(source_value)
        if target_value is not None:
            values[relationship.source_column_key] = target_value
            if impact_collector is not None:
                impact_collector.record(
                    dataset=plan.dataset_name,
                    source_row=source_row,
                    source_column=relationship.source_label,
                    target_field=relationship.target_field,
                    raw_value=raw_value,
                    proposed_value=target_value,
                    rules=relationship.rules,
                    outcome=(
                        "changed"
                        if not _display_values_equal(raw_value, target_value)
                        else "unchanged"
                    ),
                )


def _transformation_outcome(
    field,
    raw_value: object,
    proposed_value: object,
) -> str:
    if field.value_source is ScalarValueSource.CONSTANT:
        return "provided"
    if (
        field.value_source is ScalarValueSource.SOURCE_WITH_FALLBACK
        and _fallback_was_used(field, raw_value)
    ):
        return "fallback"
    if proposed_value is None and raw_value is not None:
        return "null"
    if not _display_values_equal(raw_value, proposed_value):
        return "changed"
    return "unchanged"


def _fallback_was_used(field, raw_value: object) -> bool:
    if raw_value is None:
        return True
    value = str(raw_value)
    if field.transform.trim:
        value = value.strip()
    if field.transform.collapse_whitespace:
        value = " ".join(value.split())
    return field.transform.empty_as_null and value == ""


def _attach_preparation_issues(
    prepared: PreparedBundle,
    issues: Iterable[Issue],
) -> PreparedBundle:
    by_row: dict[tuple[str, int], list[Issue]] = {}
    for issue in issues:
        if issue.dataset is None or issue.row is None:
            continue
        by_row.setdefault((issue.dataset, issue.row), []).append(issue)
    return PreparedBundle(
        records=tuple(
            replace(
                record,
                issues=(
                    *record.issues,
                    *by_row.get((record.dataset, record.source_row), ()),
                ),
            )
            for record in prepared.records
        ),
        issues=prepared.issues,
        source_hashes=prepared.source_hashes,
    )


def _normalized_key(value: object) -> str | None:
    if value is None:
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", str(value)).split())
    return normalized or None
